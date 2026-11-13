//! 暂时性失败的重试节奏（模型接口的运行、Web Push 推送共用一份）：间隔从 first 起翻倍到 max，从第一次失败起超过 window
//! 就放弃。

use std::time::Duration;

use tokio::time::Instant;

#[derive(Debug, Clone, Copy)]
pub struct Retry {
    pub first: Duration,
    pub max: Duration,
    pub window: Duration,
}

impl Default for Retry {
    /// 中转站与跨境 HTTPS 断过十几到三十分钟
    fn default() -> Self {
        Self { first: Duration::from_secs(5), max: Duration::from_secs(300), window: Duration::from_secs(30 * 60) }
    }
}

impl Retry {
    /// 开始一串尝试。
    pub fn start(self) -> Backoff {
        Backoff { retry: self, first_failure: None, delay: self.first }
    }
}

/// 一串尝试走到哪了。
#[derive(Debug)]
pub struct Backoff {
    retry: Retry,
    first_failure: Option<Instant>,
    delay: Duration,
}

impl Backoff {
    /// 又暂时失败了一次：还在窗口里就返回这次等多久再试（下一次的间隔翻倍），超出窗口返回 None（放弃）。
    pub fn next_delay(&mut self) -> Option<Duration> {
        let since = *self.first_failure.get_or_insert_with(Instant::now);
        if since.elapsed() >= self.retry.window {
            return None;
        }
        let delay = self.delay;
        self.delay = (delay * 2).min(self.retry.max);
        Some(delay)
    }

    /// 有进展了：窗口和间隔从头算。
    pub fn reset(&mut self) {
        *self = self.retry.start();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn doubles_up_to_max_and_gives_up_after_the_window() {
        let retry =
            Retry { first: Duration::from_secs(1), max: Duration::from_secs(3), window: Duration::from_secs(60) };
        let mut b = retry.start();
        let waited: Vec<u64> = (0..4).map(|_| b.next_delay().unwrap().as_secs()).collect();
        assert_eq!(waited, [1, 2, 3, 3]);
        b.reset();
        assert_eq!(b.next_delay(), Some(Duration::from_secs(1)));
        // 窗口用完就放弃
        let mut over = Retry { window: Duration::ZERO, ..retry }.start();
        assert_eq!(over.next_delay(), None);
    }
}
