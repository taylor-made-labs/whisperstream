//! Opt-in backend ownership for a locally bundled debug Mac app.
//!
//! The bundle's real GUI executable must remain the backend's parent so macOS
//! attributes native permissions to the app. Ordinary `tauri dev` has no config
//! resource and continues to use its separately launched development backend.

use crate::engine_http::{new_generation_token, probe_health, request_shutdown, HealthProbe};
use serde::Deserialize;
use std::ffi::OsString;
use std::fs::{self, OpenOptions};
use std::io;
use std::net::TcpStream;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Component, Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

const POLL_INTERVAL: Duration = Duration::from_millis(100);
const STARTUP_ATTEMPTS: usize = 300;
const SHUTDOWN_ATTEMPTS: usize = 30;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct SourceConfig {
    source_root: PathBuf,
    data_dir: PathBuf,
    no_hotkeys: bool,
}

fn configuration_path(executable: &Path) -> Option<PathBuf> {
    let macos = executable.parent()?;
    let contents = macos.parent()?;
    let bundle = contents.parent()?;
    (macos.file_name()? == "MacOS"
        && contents.file_name()? == "Contents"
        && bundle.extension()? == "app")
        .then(|| contents.join("Resources/source-backend.json"))
}

fn absolute_path(path: &Path) -> bool {
    path.is_absolute()
        && !path
            .components()
            .any(|component| matches!(component, Component::ParentDir))
}

// Resolve existing ancestors too, so a not-yet-created directory cannot reach
// installed-app state through a symlink in one of its parents.
fn resolve_destination(path: &Path) -> io::Result<PathBuf> {
    match path.canonicalize() {
        Ok(resolved) => Ok(resolved),
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            let parent = path
                .parent()
                .ok_or_else(|| io::Error::other("source backend data directory has no parent"))?;
            let name = path
                .file_name()
                .ok_or_else(|| io::Error::other("source backend data directory has no name"))?;
            Ok(resolve_destination(parent)?.join(name))
        }
        Err(error) => Err(error),
    }
}

impl SourceConfig {
    fn validate(self, installed_data_dir: &Path) -> io::Result<Self> {
        if !absolute_path(&self.source_root) || !absolute_path(&self.data_dir) {
            return Err(io::Error::other(
                "source backend paths must be absolute and contain no parent traversal",
            ));
        }
        let source_root = self.source_root.canonicalize()?;
        if !source_root.join("pyproject.toml").is_file()
            || !source_root.join("src/whisperstream/main.py").is_file()
            || !source_root.join(".venv/bin/python").is_file()
        {
            return Err(io::Error::other(
                "source backend requires a bootstrapped Whisperstream source checkout",
            ));
        }
        let data_dir = resolve_destination(&self.data_dir)?;
        let installed_data_dir = resolve_destination(installed_data_dir)?;
        if data_dir.starts_with(installed_data_dir) {
            return Err(io::Error::other(
                "source backend requires a separate development data directory",
            ));
        }
        Ok(Self {
            source_root,
            data_dir,
            no_hotkeys: self.no_hotkeys,
        })
    }

    fn arguments(&self, owner_pid: u32, generation: &str) -> Vec<OsString> {
        let mut args = vec![
            "-m".into(),
            "whisperstream.main".into(),
            "--serve".into(),
            "--port".into(),
            "38420".into(),
            "--owner-pid".into(),
            owner_pid.to_string().into(),
            "--engine-generation".into(),
            generation.into(),
        ];
        if self.no_hotkeys {
            args.push("--no-hotkeys".into());
        }
        args
    }

    fn command(&self, owner_pid: u32, generation: &str) -> Command {
        let mut command = Command::new(self.source_root.join(".venv/bin/python"));
        command
            .args(self.arguments(owner_pid, generation))
            .current_dir(&self.source_root)
            .env("WHISPERSTREAM_DATA_DIR", &self.data_dir)
            .env("WHISPERSTREAM_AGENT_PLUGIN_VARIANT", "dev")
            .env("PYTHONPATH", self.source_root.join("src"))
            .env_remove("PYTHONHOME")
            .stdin(Stdio::null());
        command
    }
}

trait OwnedChild {
    fn has_exited(&mut self) -> io::Result<bool>;
    fn kill_and_reap(&mut self);
}

impl OwnedChild for Child {
    fn has_exited(&mut self) -> io::Result<bool> {
        self.try_wait().map(|status| status.is_some())
    }

    fn kill_and_reap(&mut self) {
        let _ = self.kill();
        let _ = self.wait();
    }
}

fn await_ready(
    child: &mut impl OwnedChild,
    mut probe: impl FnMut() -> HealthProbe,
    mut pause: impl FnMut(Duration),
    attempts: usize,
) -> io::Result<()> {
    for _ in 0..attempts {
        if child.has_exited()? {
            return Err(io::Error::other(
                "source backend exited before becoming ready",
            ));
        }
        if probe() == HealthProbe::Ready && !child.has_exited()? {
            return Ok(());
        }
        pause(POLL_INTERVAL);
    }
    Err(io::Error::new(
        io::ErrorKind::TimedOut,
        "source backend did not become ready; check its development log",
    ))
}

fn stop_owned_child(
    child: &mut impl OwnedChild,
    mut request: impl FnMut(),
    mut pause: impl FnMut(Duration),
    attempts: usize,
) {
    if child.has_exited().unwrap_or(false) {
        return;
    }
    request();
    for _ in 0..attempts {
        if child.has_exited().unwrap_or(false) {
            return;
        }
        pause(POLL_INTERVAL);
    }
    child.kill_and_reap();
}

pub(crate) struct SourceBackend {
    child: Mutex<Option<Child>>,
    generation: String,
}

impl SourceBackend {
    pub(crate) fn start_if_configured() -> io::Result<Option<Self>> {
        let Some(path) = configuration_path(&std::env::current_exe()?) else {
            return Ok(None);
        };
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
            Err(error) => return Err(error),
        };
        if !metadata.file_type().is_file() || metadata.len() > 16_384 {
            return Err(io::Error::other(
                "invalid source backend configuration resource",
            ));
        }
        let config: SourceConfig = serde_json::from_slice(&fs::read(path)?)?;
        let home = std::env::var_os("HOME").ok_or_else(|| {
            io::Error::other("cannot locate installed application data directory")
        })?;
        let config = config
            .validate(&PathBuf::from(home).join("Library/Application Support/Whisperstream"))?;

        // Never attach to or stop an already-running development/installed engine.
        match TcpStream::connect_timeout(&"127.0.0.1:38420".parse().unwrap(), POLL_INTERVAL) {
            Ok(_) => {
                return Err(io::Error::new(
                    io::ErrorKind::AddrInUse,
                    "port 38420 is already in use; stop the existing backend first",
                ));
            }
            Err(error) if error.kind() == io::ErrorKind::ConnectionRefused => {}
            Err(error) => return Err(error),
        }

        let log_dir = config.data_dir.join("logs");
        fs::create_dir_all(&log_dir)?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .mode(0o600)
            .open(log_dir.join("source-backend.log"))?;
        let generation = new_generation_token(1);
        let mut command = config.command(std::process::id(), &generation);
        command
            .stdout(Stdio::from(log.try_clone()?))
            .stderr(Stdio::from(log));
        let child = command.spawn()?;
        eprintln!(
            "[tauri] source backend spawned pid={} parent_pid={}",
            child.id(),
            std::process::id()
        );
        let backend = Self {
            child: Mutex::new(Some(child)),
            generation,
        };
        {
            let mut owned = backend
                .child
                .lock()
                .map_err(|_| io::Error::other("source backend ownership unavailable"))?;
            await_ready(
                owned.as_mut().unwrap(),
                || probe_health(POLL_INTERVAL, Some(&backend.generation)),
                std::thread::sleep,
                STARTUP_ATTEMPTS,
            )?;
        }
        eprintln!("[tauri] source backend ready");
        Ok(Some(backend))
    }

    pub(crate) fn shutdown(&self) {
        let Ok(mut owned) = self.child.lock() else {
            return;
        };
        let Some(mut child) = owned.take() else {
            return;
        };
        stop_owned_child(
            &mut child,
            || {
                request_shutdown(POLL_INTERVAL, Some(&self.generation));
            },
            std::thread::sleep,
            SHUTDOWN_ATTEMPTS,
        );
    }
}

impl Drop for SourceBackend {
    fn drop(&mut self) {
        self.shutdown();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::VecDeque;

    #[derive(Default)]
    struct FakeChild {
        exited: VecDeque<bool>,
        killed: bool,
    }

    impl OwnedChild for FakeChild {
        fn has_exited(&mut self) -> io::Result<bool> {
            Ok(self.exited.pop_front().unwrap_or(false))
        }

        fn kill_and_reap(&mut self) {
            self.killed = true;
        }
    }

    fn config(no_hotkeys: bool) -> SourceConfig {
        SourceConfig {
            source_root: "/a checkout/source".into(),
            data_dir: "/isolated development/data".into(),
            no_hotkeys,
        }
    }

    #[test]
    fn only_an_app_bundle_can_opt_in() {
        assert_eq!(
            configuration_path(Path::new(
                "/local/Whisperstream.app/Contents/MacOS/whisperstream"
            )),
            Some("/local/Whisperstream.app/Contents/Resources/source-backend.json".into())
        );
        assert!(configuration_path(Path::new("/source/target/debug/whisperstream")).is_none());
        assert!(configuration_path(Path::new("/local/Contents/MacOS/whisperstream")).is_none());
    }

    #[test]
    fn configuration_rejects_unknown_fields_and_requires_explicit_hotkey_choice() {
        assert!(serde_json::from_str::<SourceConfig>(
            r#"{"source_root":"/source","data_dir":"/dev"}"#
        )
        .is_err());
        assert!(serde_json::from_str::<SourceConfig>(
            r#"{"source_root":"/source","data_dir":"/dev","no_hotkeys":true,"command":"sh"}"#
        )
        .is_err());
        assert!(!absolute_path(Path::new("relative/path")));
        assert!(!absolute_path(Path::new("/source/../installed")));
    }

    #[test]
    fn validation_rejects_installed_data_and_symlinked_ancestors() {
        let fixture = std::env::temp_dir().join(format!(
            "whisperstream-source-backend-test-{}",
            new_generation_token(1)
        ));
        let source = fixture.join("source");
        let installed = fixture.join("installed");
        fs::create_dir_all(source.join("src/whisperstream")).unwrap();
        fs::create_dir_all(source.join(".venv/bin")).unwrap();
        fs::create_dir_all(&installed).unwrap();
        fs::write(source.join("pyproject.toml"), "").unwrap();
        fs::write(source.join("src/whisperstream/main.py"), "").unwrap();
        fs::write(source.join(".venv/bin/python"), "").unwrap();
        let make_config = |data_dir| SourceConfig {
            source_root: source.clone(),
            data_dir,
            no_hotkeys: true,
        };
        assert!(make_config(installed.join("nested"))
            .validate(&installed)
            .is_err());
        let alias = fixture.join("alias");
        std::os::unix::fs::symlink(&installed, &alias).unwrap();
        assert!(make_config(alias.join("not-created-yet"))
            .validate(&installed)
            .is_err());
        assert!(make_config(fixture.join("separate/new-profile"))
            .validate(&installed)
            .is_ok());
        fs::remove_dir_all(fixture).unwrap();
    }

    #[test]
    fn command_spawns_python_directly_with_exact_owner_and_isolated_environment() {
        let command = config(true).command(123, "this-generation");
        assert_eq!(command.get_program(), "/a checkout/source/.venv/bin/python");
        assert_eq!(
            command.get_current_dir(),
            Some(Path::new("/a checkout/source"))
        );
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            [
                "-m",
                "whisperstream.main",
                "--serve",
                "--port",
                "38420",
                "--owner-pid",
                "123",
                "--engine-generation",
                "this-generation",
                "--no-hotkeys"
            ]
        );
        let environment = command
            .get_envs()
            .collect::<std::collections::BTreeMap<_, _>>();
        assert_eq!(
            environment.get(std::ffi::OsStr::new("WHISPERSTREAM_DATA_DIR")),
            Some(&Some(std::ffi::OsStr::new("/isolated development/data")))
        );
        assert_eq!(
            environment.get(std::ffi::OsStr::new("WHISPERSTREAM_AGENT_PLUGIN_VARIANT")),
            Some(&Some(std::ffi::OsStr::new("dev")))
        );
        assert!(!config(false)
            .arguments(123, "this-generation")
            .contains(&"--no-hotkeys".into()));
    }

    #[test]
    fn readiness_rejects_a_different_generation_and_reports_early_exit() {
        let mut child = FakeChild::default();
        let result = await_ready(&mut child, || HealthProbe::InvalidResponse, |_| {}, 2);
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::TimedOut);
        let mut child = FakeChild {
            exited: [true].into(),
            ..Default::default()
        };
        assert!(await_ready(
            &mut child,
            || panic!("exited child must not be probed"),
            |_| {},
            2
        )
        .is_err());
    }

    #[test]
    fn readiness_accepts_only_the_live_owned_child() {
        let mut child = FakeChild::default();
        assert!(await_ready(&mut child, || HealthProbe::Ready, |_| {}, 2).is_ok());
        let mut child = FakeChild {
            exited: [false, true, true].into(),
            ..Default::default()
        };
        assert!(await_ready(&mut child, || HealthProbe::Ready, |_| {}, 2).is_err());
    }

    #[test]
    fn shutdown_does_nothing_after_owned_child_has_exited() {
        let mut child = FakeChild {
            exited: [true].into(),
            ..Default::default()
        };
        stop_owned_child(
            &mut child,
            || panic!("no request after child exit"),
            |_| {},
            2,
        );
        assert!(!child.killed);
    }

    #[test]
    fn shutdown_is_bounded_and_only_escalates_for_its_owned_child() {
        let mut requests = 0;
        let mut child = FakeChild {
            exited: [false, false, true].into(),
            ..Default::default()
        };
        stop_owned_child(&mut child, || requests += 1, |_| {}, 2);
        assert_eq!(requests, 1);
        assert!(!child.killed);
        let mut child = FakeChild::default();
        stop_owned_child(&mut child, || requests += 1, |_| {}, 2);
        assert_eq!(requests, 2);
        assert!(child.killed);
    }
}
