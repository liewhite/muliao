//! 单用户认证：密码登录后下发 HMAC 签名 cookie；登录失败按来源 IP 退避（#540）。
//!
//! 签名 key 由配置的 secret_key 与密码的摘要一起派生：改了密码（改配置、重启），之前签发的 cookie 全部失效，不需要服务端的
//! 会话表。登出只删浏览器里的 cookie。算法与 cookie 格式和 Python 版相同：切换后已登录的浏览器、收藏的页面地址照样能用。

use std::collections::VecDeque;
use std::net::{IpAddr, SocketAddr};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::extract::{ConnectInfo, Request, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::middleware::Next;
use axum::response::{IntoResponse, Response};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use subtle::ConstantTimeEq;
use utoipa::ToSchema;

use crate::error::{ApiError, ApiResult, ErrorOut};
use crate::state::AppState;

pub const COOKIE_NAME: &str = "muliao_session";
pub const MAX_AGE_SECONDS: u64 = 90 * 24 * 3600;
const VERSION: &str = "v1";

pub struct Auth {
    password: Vec<u8>,
    key: Vec<u8>,
}

fn hmac(key: &[u8], message: &[u8]) -> Vec<u8> {
    let mut mac = Hmac::<Sha256>::new_from_slice(key).expect("HMAC takes keys of any length");
    mac.update(message);
    mac.finalize().into_bytes().to_vec()
}

fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// 恒定时间比较外部给的值
fn same(given: &str, expected: &str) -> bool {
    given.as_bytes().ct_eq(expected.as_bytes()).into()
}

fn now_seconds() -> u64 {
    SystemTime::now().duration_since(UNIX_EPOCH).expect("the clock is after 1970").as_secs()
}

impl Auth {
    pub fn new(secret_key: &str, password: &str) -> Self {
        let key = hmac(secret_key.as_bytes(), &Sha256::digest(password.as_bytes()));
        Self { password: password.as_bytes().to_vec(), key }
    }

    fn sign(&self, payload: &str) -> String {
        hex(&hmac(&self.key, payload.as_bytes()))
    }

    pub fn check_password(&self, password: &str) -> bool {
        password.as_bytes().ct_eq(&self.password).into()
    }

    pub fn issue(&self, now: u64) -> String {
        let payload = format!("{VERSION}.{now}");
        format!("{payload}.{}", self.sign(&payload))
    }

    pub fn verify(&self, token: &str, now: u64) -> bool {
        let parts: Vec<&str> = token.split('.').collect();
        let [version, issued, sig] = parts[..] else { return false };
        if version != VERSION || issued.is_empty() || !issued.bytes().all(|b| b.is_ascii_digit()) {
            return false;
        }
        if !same(sig, &self.sign(&format!("{version}.{issued}"))) {
            return false;
        }
        issued.parse::<u64>().is_ok_and(|issued| now.saturating_sub(issued) < MAX_AGE_SECONDS)
    }

    /// agent 页面的签名网址段（[`crate::api::sites`]）：页面在沙箱里跑、拿不到登录 cookie，凭它访问；与 cookie 同一把
    /// key，改了密码一并失效。
    pub fn sign_site(&self, agent_id: &str, page: &str) -> String {
        self.sign(&format!("site.{agent_id}/{page}"))
    }

    pub fn verify_site(&self, agent_id: &str, page: &str, sig: &str) -> bool {
        same(sig, &self.sign_site(agent_id, page))
    }
}

/// 登录失败按来源 IP 退避：同一 IP 连续输错，从第 FREE_FAILURES 次起每次输错后都要等一段时间才能再试，等待时间从
/// BASE_DELAY 起翻倍、最长 MAX_DELAY；等待期内连正确密码也拒绝（不校验密码），登录成功清零，最后一次失败过了 FORGET_AFTER
/// 也清零。只在内存里：重启清零，可以接受。
#[derive(Default)]
pub struct LoginThrottle {
    /// (ip, 连续失败次数, 最后一次失败的时刻)，按最后一次失败的时刻排序：从表头清掉已遗忘的条目，换着 IP 试也不无限增长
    failures: Mutex<VecDeque<(IpAddr, u32, Instant)>>,
}

impl LoginThrottle {
    pub const FREE_FAILURES: u32 = 5;
    pub const BASE_DELAY: Duration = Duration::from_secs(30);
    pub const MAX_DELAY: Duration = Duration::from_secs(15 * 60);
    pub const FORGET_AFTER: Duration = Duration::from_secs(3600);

    fn current(entries: &VecDeque<(IpAddr, u32, Instant)>, ip: IpAddr, now: Instant) -> Option<(u32, Instant)> {
        entries
            .iter()
            .find(|e| e.0 == ip)
            .filter(|e| now.saturating_duration_since(e.2) < Self::FORGET_AFTER)
            .map(|e| (e.1, e.2))
    }

    /// 还要等多久才能再试；零表示现在可以试。
    pub fn retry_after(&self, ip: IpAddr, now: Instant) -> Duration {
        let entries = self.failures.lock().expect("throttle lock");
        let Some((count, last)) = Self::current(&entries, ip, now) else { return Duration::ZERO };
        if count < Self::FREE_FAILURES {
            return Duration::ZERO;
        }
        let doublings = (count - Self::FREE_FAILURES).min(16);
        let delay = (Self::BASE_DELAY * 2u32.pow(doublings)).min(Self::MAX_DELAY);
        (last + delay).saturating_duration_since(now)
    }

    pub fn failed(&self, ip: IpAddr, now: Instant) {
        let mut entries = self.failures.lock().expect("throttle lock");
        let count = Self::current(&entries, ip, now).map_or(0, |c| c.0) + 1;
        entries.retain(|e| e.0 != ip);
        while entries.front().is_some_and(|e| now.saturating_duration_since(e.2) >= Self::FORGET_AFTER) {
            entries.pop_front();
        }
        entries.push_back((ip, count, now));
    }

    pub fn succeeded(&self, ip: IpAddr) {
        self.failures.lock().expect("throttle lock").retain(|e| e.0 != ip);
    }
}

/// 请求的来源 IP：对端是本机时（本机 userspace tailscaled、公网入口经它转进来的连接）采信 X-Forwarded-For 里从右往左第一个
/// 不是本机的地址，同 uvicorn 的 proxy_headers（受信代理只有本机）。tailnet 成员算可信：它们直连时能自填这个头。
pub fn client_ip(peer: SocketAddr, headers: &HeaderMap) -> IpAddr {
    let peer = peer.ip();
    if !peer.is_loopback() {
        return peer;
    }
    let forwarded = headers.get_all("x-forwarded-for").iter().filter_map(|v| v.to_str().ok()).collect::<Vec<_>>();
    forwarded
        .join(",")
        .rsplit(',')
        .filter_map(|s| s.trim().parse::<IpAddr>().ok())
        .find(|ip| !ip.is_loopback())
        .unwrap_or(peer)
}

fn cookie<'h>(headers: &'h HeaderMap, name: &str) -> Option<&'h str> {
    headers
        .get_all(header::COOKIE)
        .iter()
        .filter_map(|v| v.to_str().ok())
        .flat_map(|v| v.split(';'))
        .filter_map(|pair| pair.trim().split_once('='))
        .find(|(k, _)| *k == name)
        .map(|(_, v)| v.trim_matches('"'))
}

/// 登录后才能访问的接口前面的一层：没有有效的 cookie 就 401。
pub async fn require_auth(State(state): State<AppState>, request: Request, next: Next) -> Response {
    match cookie(request.headers(), COOKIE_NAME) {
        Some(token) if state.auth.verify(token, now_seconds()) => next.run(request).await,
        _ => ApiError::new(StatusCode::UNAUTHORIZED, "not authenticated").into_response(),
    }
}

#[derive(Deserialize, ToSchema)]
pub struct LoginIn {
    pub password: String,
}

fn set_cookie(value: &str, max_age: u64, secure: bool) -> HeaderValue {
    let secure = if secure { "; Secure" } else { "" };
    HeaderValue::from_str(&format!("{COOKIE_NAME}={value}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=lax{secure}"))
        .expect("the cookie is ASCII")
}

/// 用密码登录，成功后下发登录 cookie。连续输错同一 IP 要等一段时间才能再试（429，带 Retry-After）。
#[utoipa::path(post, path = "/api/auth/login", tag = "auth", request_body = LoginIn,
    responses((status = 204, description = "已登录"), (status = 401, body = ErrorOut), (status = 429, body = ErrorOut)))]
pub async fn login(
    State(state): State<AppState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    headers: HeaderMap,
    Json(body): Json<LoginIn>,
) -> ApiResult<Response> {
    let ip = client_ip(peer, &headers);
    let now = Instant::now();
    let wait = state.throttle.retry_after(ip, now);
    if !wait.is_zero() {
        // 退避期内被拒的不改计数，有人狂刷时不让它刷满日志；进入退避的那次失败已有 warning
        tracing::debug!(client = %ip, retry_after = wait.as_secs(), "login throttled");
        return Err(ApiError::throttled(wait.as_secs_f64().ceil() as u64));
    }
    if !state.auth.check_password(&body.password) {
        state.throttle.failed(ip, now);
        tracing::warn!(client = %ip, "login failed");
        return Err(ApiError::new(StatusCode::UNAUTHORIZED, "wrong password"));
    }
    state.throttle.succeeded(ip);
    let cookie = set_cookie(&state.auth.issue(now_seconds()), MAX_AGE_SECONDS, state.secure_cookie);
    Ok((StatusCode::NO_CONTENT, [(header::SET_COOKIE, cookie)]).into_response())
}

/// 登出：删掉浏览器里的登录 cookie。
#[utoipa::path(post, path = "/api/auth/logout", tag = "auth", responses((status = 204, description = "已登出")))]
pub async fn logout(State(state): State<AppState>) -> Response {
    (StatusCode::NO_CONTENT, [(header::SET_COOKIE, set_cookie("", 0, state.secure_cookie))]).into_response()
}

/// 已登录时 204，否则 401（前端据此判断要不要显示登录页）。
#[utoipa::path(get, path = "/api/auth/me", tag = "auth", responses((status = 204, description = "已登录"), (status = 401, body = ErrorOut)))]
pub async fn me() -> StatusCode {
    StatusCode::NO_CONTENT
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cookie_round_trip_and_python_compatible() {
        let auth = Auth::new("0123456789abcdef0123456789abcdef", "hunter22");
        let token = auth.issue(1_700_000_000);
        assert!(auth.verify(&token, 1_700_000_000 + 10));
        assert!(!auth.verify(&token, 1_700_000_000 + MAX_AGE_SECONDS));
        assert!(!auth.verify(&token.replace("v1.", "v2."), 1_700_000_000));
        assert!(!auth.verify("v1.x.y", 1_700_000_000));
        assert!(!auth.verify(&format!("{token}0"), 1_700_000_000));
        assert!(!Auth::new("0123456789abcdef0123456789abcdef", "other-pw").verify(&token, 1_700_000_000));
        // 与 Python 版（api/auth.py）同一输入算出的值：
        // Auth("0123456789abcdef0123456789abcdef", "hunter22").issue(1700000000)
        assert_eq!(token, "v1.1700000000.b5193bfa636491addd81bb3ed539cc2f81c17f8217caced64cf29addf14834fa");
        assert!(auth.verify_site("a1", "dash", &auth.sign_site("a1", "dash")));
        assert!(!auth.verify_site("a1", "other", &auth.sign_site("a1", "dash")));
        assert!(auth.check_password("hunter22") && !auth.check_password("hunter2"));
    }

    #[test]
    fn throttle_backs_off_then_forgets() {
        let t = LoginThrottle::default();
        let ip: IpAddr = "1.2.3.4".parse().unwrap();
        let other: IpAddr = "5.6.7.8".parse().unwrap();
        let t0 = Instant::now();
        for _ in 0..LoginThrottle::FREE_FAILURES - 1 {
            t.failed(ip, t0);
        }
        assert_eq!(t.retry_after(ip, t0), Duration::ZERO);
        t.failed(ip, t0);
        assert_eq!(t.retry_after(ip, t0), LoginThrottle::BASE_DELAY);
        assert_eq!(t.retry_after(other, t0), Duration::ZERO);
        let t1 = t0 + LoginThrottle::BASE_DELAY;
        assert_eq!(t.retry_after(ip, t1), Duration::ZERO);
        t.failed(ip, t1);
        assert_eq!(t.retry_after(ip, t1), LoginThrottle::BASE_DELAY * 2);
        for _ in 0..20 {
            t.failed(ip, t1);
        }
        assert_eq!(t.retry_after(ip, t1), LoginThrottle::MAX_DELAY);
        assert_eq!(t.retry_after(ip, t1 + LoginThrottle::FORGET_AFTER), Duration::ZERO);
        t.succeeded(ip);
        assert_eq!(t.retry_after(ip, t1), Duration::ZERO);
    }

    #[test]
    fn client_ip_trusts_forwarded_only_from_loopback() {
        let mut h = HeaderMap::new();
        h.insert("x-forwarded-for", "9.9.9.9, 8.8.8.8".parse().unwrap());
        assert_eq!(client_ip("127.0.0.1:1".parse().unwrap(), &h), "8.8.8.8".parse::<IpAddr>().unwrap());
        assert_eq!(client_ip("10.0.0.2:1".parse().unwrap(), &h), "10.0.0.2".parse::<IpAddr>().unwrap());
        assert_eq!(
            client_ip("127.0.0.1:1".parse().unwrap(), &HeaderMap::new()),
            "127.0.0.1".parse::<IpAddr>().unwrap()
        );
    }

    #[test]
    fn reads_the_cookie() {
        let mut h = HeaderMap::new();
        h.insert(header::COOKIE, "a=1; muliao_session=v1.2.x; b=2".parse().unwrap());
        assert_eq!(cookie(&h, COOKIE_NAME), Some("v1.2.x"));
        assert_eq!(cookie(&h, "c"), None);
    }
}
