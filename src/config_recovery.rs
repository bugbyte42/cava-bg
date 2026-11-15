use crate::app_config::Config;
use anyhow::{Context, Result};
use parking_lot::Mutex;
use std::fs;
use std::path::Path;
use std::sync::Arc;

/// Retain accepted config changes across renderer failures.
#[derive(Clone)]
pub(crate) struct ConfigRecovery {
    last_good: Arc<Mutex<Config>>,
    output_filter: Option<String>,
}

impl ConfigRecovery {
    pub(crate) fn new(config: Config, output_filter: Option<String>) -> Self {
        Self {
            last_good: Arc::new(Mutex::new(prepare(config, output_filter.as_deref()))),
            output_filter,
        }
    }

    pub(crate) fn latest(&self) -> Config {
        self.last_good.lock().clone()
    }

    pub(crate) fn remember(&self, config: Config) -> Config {
        let config = prepare(config, self.output_filter.as_deref());
        *self.last_good.lock() = config.clone();
        config
    }

    pub(crate) fn reload(&self, path: &Path) -> Result<Config> {
        let content = fs::read_to_string(path)
            .with_context(|| format!("Unable to read config file: {}", path.display()))?;
        let config = toml::from_str::<Config>(&content)
            .with_context(|| format!("Unable to parse config file: {}", path.display()))?;
        Ok(self.remember(config))
    }
}

fn prepare(mut config: Config, output_filter: Option<&str>) -> Config {
    config.normalize_compat_fields();
    if let Some(output) = output_filter {
        config.general.preferred_outputs = vec![output.to_string()];
    }
    config
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::app_config::{serialize_config, AudioConfig, OutputDescriptor, VisualizationMode};
    use std::panic::{catch_unwind, AssertUnwindSafe};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicUsize, Ordering};

    struct Fixture(PathBuf);

    impl Fixture {
        fn new() -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let id = NEXT.fetch_add(1, Ordering::Relaxed);
            let dir = std::env::temp_dir().join(format!(
                "cava-bg-config-recovery-{}-{id}",
                std::process::id()
            ));
            fs::create_dir(&dir).unwrap();
            Self(dir)
        }

        fn path(&self) -> PathBuf {
            self.0.join("config.toml")
        }

        fn write(&self, config: &Config) {
            fs::write(self.path(), serialize_config(config).unwrap()).unwrap();
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn config(count: u32, mode: VisualizationMode) -> Config {
        Config {
            audio: AudioConfig {
                bar_count: count,
                visualization_mode: mode,
                ..AudioConfig::default()
            },
            ..Config::default()
        }
    }

    #[test]
    fn recovery_reads_the_current_file_instead_of_startup_config() {
        let fixture = Fixture::new();
        let initial = config(128, VisualizationMode::Bars);
        fixture.write(&initial);
        let recovery = ConfigRecovery::new(initial, None);
        fixture.write(&config(512, VisualizationMode::Ring));

        let restarted = recovery.reload(&fixture.path()).unwrap();
        assert_eq!(restarted.audio.bar_count, 512);
        assert_eq!(restarted.audio.visualization_mode, VisualizationMode::Ring);
        assert_eq!(recovery.latest().audio.bar_count, 512);
    }

    #[test]
    fn accepted_hot_reload_survives_unwind_and_a_malformed_file() {
        let fixture = Fixture::new();
        let recovery = ConfigRecovery::new(config(128, VisualizationMode::Bars), None);
        let renderer_copy = recovery.clone();
        let result = catch_unwind(AssertUnwindSafe(move || {
            renderer_copy.remember(config(512, VisualizationMode::Ring));
            panic!("Simulated renderer failure");
        }));
        assert!(result.is_err());
        let invalid = "[audio\nbar_count = unfinished";
        fs::write(fixture.path(), invalid).unwrap();

        assert!(recovery.reload(&fixture.path()).is_err());
        let fallback = recovery.latest();
        assert_eq!(fallback.audio.bar_count, 512);
        assert_eq!(fallback.audio.visualization_mode, VisualizationMode::Ring);
        assert_eq!(fs::read_to_string(fixture.path()).unwrap(), invalid);
        assert_eq!(fs::read_dir(&fixture.0).unwrap().count(), 1);
    }

    #[test]
    fn recovery_retains_hot_reload_when_the_file_disappears() {
        let fixture = Fixture::new();
        let recovery = ConfigRecovery::new(config(128, VisualizationMode::Bars), None);
        let updated = config(256, VisualizationMode::Spectrum);
        fixture.write(&updated);
        recovery.remember(updated);
        fs::remove_file(fixture.path()).unwrap();

        assert!(recovery.reload(&fixture.path()).is_err());
        assert_eq!(recovery.latest().audio.bar_count, 256);
        assert_eq!(
            recovery.latest().audio.visualization_mode,
            VisualizationMode::Spectrum
        );
        assert!(!fixture.path().exists());
    }

    #[test]
    fn recovery_accepts_a_repaired_file_after_a_failed_read() {
        let fixture = Fixture::new();
        let recovery = ConfigRecovery::new(config(128, VisualizationMode::Bars), None);
        fs::write(fixture.path(), "[audio").unwrap();
        assert!(recovery.reload(&fixture.path()).is_err());
        assert_eq!(recovery.latest().audio.bar_count, 128);

        fixture.write(&config(64, VisualizationMode::Waveform));
        let restarted = recovery.reload(&fixture.path()).unwrap();
        assert_eq!(restarted.audio.bar_count, 64);
        assert_eq!(
            restarted.audio.visualization_mode,
            VisualizationMode::Waveform
        );
        assert_eq!(recovery.latest().audio.bar_count, 64);
    }

    #[test]
    fn recovery_preserves_cli_output_and_normalizes_each_accepted_config() {
        let fixture = Fixture::new();
        let recovery = ConfigRecovery::new(Config::default(), Some("DP-1".into()));
        assert_eq!(recovery.latest().general.preferred_outputs, ["DP-1"]);
        let text = "[general]\npreferred_outputs = ['HDMI-A-1']\n[display]\nmargin = 7.0\n\
                    [output.'DP-1'.display]\nmargin = 3.0\n";
        fs::write(fixture.path(), text).unwrap();
        let restarted = recovery.reload(&fixture.path()).unwrap();
        assert_eq!(restarted.general.preferred_outputs, ["DP-1"]);
        assert_eq!(restarted.display.margin_top, 7);
        let output = restarted
            .resolve_for_output(&OutputDescriptor {
                name: "DP-1".into(),
                connector: None,
                index: Some(0),
            })
            .unwrap();
        assert_eq!(output.display.margin_top, 3);
        assert_eq!(fs::read_to_string(fixture.path()).unwrap(), text);

        let remembered = recovery.remember(Config::default());
        assert_eq!(remembered.general.preferred_outputs, ["DP-1"]);
        assert_eq!(recovery.latest().general.preferred_outputs, ["DP-1"]);
    }
}
