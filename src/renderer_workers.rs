//! Renderer-owned polling threads that stop before the next renderer starts.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::thread::{self, JoinHandle};

pub(crate) struct RendererWorkers {
    running: Arc<AtomicBool>,
    handles: Vec<JoinHandle<()>>,
}

impl Default for RendererWorkers {
    fn default() -> Self {
        Self {
            running: Arc::new(AtomicBool::new(true)),
            handles: Vec::new(),
        }
    }
}

impl RendererWorkers {
    pub(crate) fn spawn(&mut self, worker: impl FnOnce(Arc<AtomicBool>) + Send + 'static) {
        let running = self.running.clone();
        self.handles.push(thread::spawn(move || worker(running)));
    }
}

impl Drop for RendererWorkers {
    fn drop(&mut self) {
        self.running.store(false, Ordering::SeqCst);
        // Join before restart so old listeners cannot remove replacement sockets.
        for handle in self.handles.drain(..) {
            if handle.join().is_err() {
                log::warn!("Renderer worker panicked during teardown");
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc::{channel, Receiver};
    use std::time::Duration;

    fn start_worker(workers: &mut RendererWorkers) -> Receiver<()> {
        let (started_tx, started_rx) = channel();
        let (stopped_tx, stopped_rx) = channel();
        workers.spawn(move |running| {
            started_tx.send(()).unwrap();
            while running.load(Ordering::SeqCst) {
                thread::sleep(Duration::from_millis(1));
            }
            stopped_tx.send(()).unwrap();
        });
        started_rx.recv_timeout(Duration::from_secs(5)).unwrap();
        stopped_rx
    }

    #[test]
    fn drop_stops_and_joins_every_worker() {
        let mut workers = RendererWorkers::default();
        let stopped: Vec<_> = (0..4).map(|_| start_worker(&mut workers)).collect();
        drop(workers);
        for rx in stopped {
            rx.try_recv()
                .expect("worker completed before drop returned");
        }
    }

    #[test]
    fn error_stops_workers_before_recovery() {
        fn fail(stopped: &mut Option<Receiver<()>>) -> Result<(), ()> {
            let mut workers = RendererWorkers::default();
            *stopped = Some(start_worker(&mut workers));
            Err(())
        }
        let mut stopped = None;
        let result = fail(&mut stopped);
        assert!(result.is_err());
        stopped.unwrap().try_recv().unwrap();
    }

    #[test]
    fn panic_stops_workers_before_recovery() {
        let mut stopped = None;
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let mut workers = RendererWorkers::default();
            stopped = Some(start_worker(&mut workers));
            panic!("simulated renderer failure");
        }));
        assert!(result.is_err());
        stopped.unwrap().try_recv().unwrap();
    }

    #[test]
    fn recovery_does_not_cancel_new_workers() {
        for _ in 0..8 {
            let mut workers = RendererWorkers::default();
            let stopped = start_worker(&mut workers);
            assert!(stopped.try_recv().is_err());
            drop(workers);
            stopped.try_recv().unwrap();
        }
    }

    #[test]
    fn worker_panic_does_not_skip_other_joins() {
        let mut workers = RendererWorkers::default();
        workers.spawn(|_| panic!("simulated worker failure"));
        let stopped = start_worker(&mut workers);
        drop(workers);
        stopped.try_recv().unwrap();
    }
}
