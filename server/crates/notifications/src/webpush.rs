//! Web Push 协议本身：VAPID 私钥（[`VapidKeys`]）、载荷加密（RFC 8291，aes128gcm）、VAPID 签名（RFC 8292）、发一个请求并
//! 把回应分成几类（[`Sent`]）。不认识通知与重试，那些在 [`crate::push`]。
//!
//! 没用 web-push crate：它的加密依赖 ece，ece 只有 OpenSSL 一种实现，而本仓库不链接 OpenSSL（见 Cargo.toml 的 reqwest）。
//! 协议就是一次 ECDH + HKDF + AES-GCM 和一个 ES256 签名，用 RustCrypto 写，RFC 8291 的例子在测试里逐字节核对。

use std::os::unix::fs::PermissionsExt;
use std::path::Path;
use std::time::Duration;

use aes_gcm::aead::{Aead, KeyInit};
use aes_gcm::{Aes128Gcm, Nonce};
use base64::Engine;
use base64::engine::general_purpose::URL_SAFE_NO_PAD as B64;
use hkdf::Hkdf;
use p256::ecdh::diffie_hellman;
use p256::ecdsa::signature::Signer;
use p256::ecdsa::{Signature, SigningKey};
use p256::elliptic_curve::rand_core::{OsRng, RngCore};
use p256::elliptic_curve::sec1::ToEncodedPoint;
use p256::pkcs8::{DecodePrivateKey, EncodePrivateKey, LineEnding};
use p256::{PublicKey, SecretKey};
use sha2::Sha256;

/// 推送服务保留没送到的消息多久（设备离线时）
const TTL_SECONDS: u64 = 24 * 3600;
/// VAPID 签名的有效期（RFC 8292 上限 24 小时）
const JWT_LIFETIME: Duration = Duration::from_secs(12 * 3600);
/// 一条消息就一个记录：rs 只要大于载荷即可，用 RFC 8188 的常见值
const RECORD_SIZE: u32 = 4096;
pub(crate) const REQUEST_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Debug, thiserror::Error)]
pub enum KeyError {
    #[error("VAPID key file {path}: {source}")]
    Io { path: String, source: std::io::Error },
    #[error("VAPID key file {path} is not a PKCS#8 P-256 private key: {detail}")]
    Format { path: String, detail: String },
}

/// 推送用的 VAPID 私钥（平台私有文件，沿用 Python 版生成的那一个，换实现不用设备重新订阅）。
pub struct VapidKeys {
    signing: SigningKey,
    /// 浏览器 `PushManager.subscribe` 的 applicationServerKey：未压缩公钥的 base64url
    public_key: String,
}

impl VapidKeys {
    /// 读私钥；文件不存在就生成一个（0600）。别人可读时收回到 0600。
    pub fn load_or_create(path: &Path) -> Result<Self, KeyError> {
        let io = |source| KeyError::Io { path: path.display().to_string(), source };
        if !path.exists() {
            let key = SecretKey::random(&mut OsRng);
            let pem = key
                .to_pkcs8_pem(LineEnding::LF)
                .map_err(|e| KeyError::Format { path: path.display().to_string(), detail: e.to_string() })?;
            if let Some(dir) = path.parent() {
                std::fs::create_dir_all(dir).map_err(io)?;
            }
            write_private(path, pem.as_bytes()).map_err(io)?;
            tracing::info!(path = %path.display(), "generated VAPID key");
        }
        let mode = std::fs::metadata(path).map_err(io)?.permissions().mode();
        if mode & 0o077 != 0 {
            tracing::warn!(path = %path.display(), "VAPID private key was readable by others, restricting to 0600");
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600)).map_err(io)?;
        }
        let pem = std::fs::read_to_string(path).map_err(io)?;
        let key = SecretKey::from_pkcs8_pem(&pem)
            .map_err(|e| KeyError::Format { path: path.display().to_string(), detail: e.to_string() })?;
        Ok(Self::from_secret(key))
    }

    fn from_secret(key: SecretKey) -> Self {
        let public_key = B64.encode(key.public_key().to_encoded_point(false).as_bytes());
        Self { signing: SigningKey::from(key), public_key }
    }

    pub fn public_key(&self) -> &str {
        &self.public_key
    }

    /// RFC 8292 的 Authorization 头：对推送服务的 origin 签一个 ES256 JWT。
    fn authorization(&self, endpoint: &reqwest::Url, subject: &str) -> String {
        let audience = endpoint.origin().ascii_serialization();
        let expires = (std::time::SystemTime::now() + JWT_LIFETIME)
            .duration_since(std::time::UNIX_EPOCH)
            .expect("the clock is after 1970")
            .as_secs();
        let header = B64.encode(r#"{"typ":"JWT","alg":"ES256"}"#);
        let claims = B64.encode(serde_json::json!({"aud": audience, "exp": expires, "sub": subject}).to_string());
        let signed = format!("{header}.{claims}");
        let signature: Signature = self.signing.sign(signed.as_bytes());
        format!("vapid t={signed}.{}, k={}", B64.encode(signature.to_bytes()), self.public_key)
    }
}

/// 新建的私钥文件一开始就是 0600（不先建再改权限）
fn write_private(path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    use std::io::Write;
    use std::os::unix::fs::OpenOptionsExt;
    let mut f = std::fs::OpenOptions::new().write(true).create_new(true).mode(0o600).open(path)?;
    f.write_all(bytes)?;
    f.sync_all()
}

/// 一台设备的推送订阅原文（浏览器给的三样，库里也存这三样）。用前 [`Subscription::parse`] 成 [`Device`]。endpoint 的完整
/// 路径即订阅凭据，不进日志。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Subscription {
    pub endpoint: String,
    /// 设备的 P-256 公钥（未压缩，base64url）
    pub p256dh: String,
    /// 16 字节的认证密钥（base64url）
    pub auth: String,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
#[error("invalid push subscription: {0}")]
pub struct InvalidDevice(pub String);

impl Subscription {
    /// 解析：endpoint 是 URL，两把密钥解得开。订阅时解析一次（坏订阅当场报给界面），推送前从库里读出时再解析。
    pub fn parse(&self) -> Result<Device, InvalidDevice> {
        let url = reqwest::Url::parse(&self.endpoint).map_err(|e| InvalidDevice(format!("endpoint: {e}")))?;
        let point = B64
            .decode(self.p256dh.trim_end_matches('='))
            .map_err(|e| InvalidDevice(format!("p256dh is not base64url: {e}")))?;
        let public =
            PublicKey::from_sec1_bytes(&point).map_err(|_| InvalidDevice("p256dh is not a P-256 public key".into()))?;
        let auth = B64
            .decode(self.auth.trim_end_matches('='))
            .map_err(|e| InvalidDevice(format!("auth is not base64url: {e}")))?;
        let auth: [u8; 16] =
            auth.try_into().map_err(|a: Vec<u8>| InvalidDevice(format!("auth must be 16 bytes, got {}", a.len())))?;
        Ok(Device { endpoint: self.endpoint.clone(), url, keys: DeviceKeys { public, auth } })
    }
}

/// 解析好的一台设备（不派生 Debug：endpoint 的完整路径是订阅凭据）。
pub struct Device {
    /// 订阅原文里的 endpoint（库里按它找这台设备）
    pub endpoint: String,
    url: reqwest::Url,
    keys: DeviceKeys,
}

/// 设备给的密钥解码后的形态
struct DeviceKeys {
    public: PublicKey,
    auth: [u8; 16],
}

impl Device {
    pub fn is_https(&self) -> bool {
        self.url.scheme() == "https"
    }

    /// 推送服务的主机（日志里只记它）
    pub fn host(&self) -> &str {
        self.url.host_str().unwrap_or_default()
    }
}

/// 按 RFC 8291 加密载荷（发送方临时密钥与盐由调用方给，测试用 RFC 的固定值核对）。返回请求体：头（盐、记录大小、
/// 发送方公钥）+ 一个记录的密文。
fn encrypt_with(device: &DeviceKeys, sender: &SecretKey, salt: &[u8; 16], payload: &[u8]) -> Vec<u8> {
    let ua_public = device.public.to_encoded_point(false);
    let as_public = sender.public_key().to_encoded_point(false);
    let shared = diffie_hellman(sender.to_nonzero_scalar(), device.public.as_affine());

    let mut key_info = b"WebPush: info\0".to_vec();
    key_info.extend_from_slice(ua_public.as_bytes());
    key_info.extend_from_slice(as_public.as_bytes());
    let mut ikm = [0u8; 32];
    Hkdf::<Sha256>::new(Some(device.auth.as_slice()), shared.raw_secret_bytes())
        .expand(&key_info, &mut ikm)
        .expect("32 bytes is a valid HKDF-SHA256 output length");

    let prk = Hkdf::<Sha256>::new(Some(salt), &ikm);
    let mut cek = [0u8; 16];
    let mut nonce = [0u8; 12];
    prk.expand(b"Content-Encoding: aes128gcm\0", &mut cek).expect("valid HKDF length");
    prk.expand(b"Content-Encoding: nonce\0", &mut nonce).expect("valid HKDF length");

    // 唯一的记录也是最后一个：明文后接分隔符 0x02，不加填充
    let mut plaintext = payload.to_vec();
    plaintext.push(2);
    let ciphertext = Aes128Gcm::new(&cek.into())
        .encrypt(Nonce::from_slice(&nonce), plaintext.as_slice())
        .expect("AES-GCM encryption of an in-memory buffer does not fail");

    let mut body = Vec::with_capacity(16 + 4 + 1 + as_public.len() + ciphertext.len());
    body.extend_from_slice(salt);
    body.extend_from_slice(&RECORD_SIZE.to_be_bytes());
    body.push(as_public.len() as u8);
    body.extend_from_slice(as_public.as_bytes());
    body.extend_from_slice(&ciphertext);
    body
}

/// 推送服务对一次投递的回应。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Sent {
    Delivered,
    /// 404/410：订阅已失效（RFC 8030），删掉它
    Gone(u16),
    /// 没拿到回应（连接、超时、读到一半断开）或 408/429/5xx：过一会儿再试
    Transient(String),
    /// 其余 4xx（VAPID 声明、时钟、载荷）多半是本端的问题：不删用户的订阅，也不重试
    Rejected(String),
}

/// 把载荷加密后推到一台设备。
pub async fn send(client: &reqwest::Client, keys: &VapidKeys, subject: &str, device: &Device, payload: &[u8]) -> Sent {
    let mut salt = [0u8; 16];
    OsRng.fill_bytes(&mut salt);
    let body = encrypt_with(&device.keys, &SecretKey::random(&mut OsRng), &salt, payload);
    let request = client
        .post(device.url.clone())
        .timeout(REQUEST_TIMEOUT)
        .header("TTL", TTL_SECONDS.to_string())
        .header("Content-Encoding", "aes128gcm")
        .header("Content-Type", "application/octet-stream")
        .header("Authorization", keys.authorization(&device.url, subject))
        .body(body);
    match request.send().await {
        // 不记错误原文：它带着 endpoint 的完整路径，即订阅凭据
        Err(e) => Sent::Transient(format!("no response from the push service ({})", transport_kind(&e))),
        Ok(response) => {
            let status = response.status();
            match status.as_u16() {
                200..=299 => Sent::Delivered,
                code @ (404 | 410) => Sent::Gone(code),
                408 | 429 | 500..=599 => Sent::Transient(format!("push service answered HTTP {status}")),
                _ => {
                    let detail = response.text().await.unwrap_or_default();
                    Sent::Rejected(format!(
                        "push service rejected the message with HTTP {status}: {}",
                        muliao_platform::text::truncate(detail.trim(), 300)
                    ))
                }
            }
        }
    }
}

fn transport_kind(e: &reqwest::Error) -> &'static str {
    if e.is_timeout() {
        "timeout"
    } else if e.is_connect() {
        "connect"
    } else if e.is_body() || e.is_decode() {
        "body"
    } else {
        "request"
    }
}

#[cfg(test)]
mod tests {
    use p256::ecdsa::VerifyingKey;
    use p256::ecdsa::signature::Verifier;

    use super::*;

    fn b64(s: &str) -> Vec<u8> {
        B64.decode(s).unwrap()
    }

    /// RFC 8291 第 5 节的例子：同样的密钥与盐得到逐字节相同的请求体
    #[test]
    fn encrypts_the_rfc_8291_example() {
        let device = Subscription {
            endpoint: "https://push.example.net/push/JzLQ3raZJfFBR0aqvOMsLrt54w4rJUsV".into(),
            p256dh: "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4".into(),
            auth: "BTBZMqHH6r4Tts7J_aSIgg".into(),
        }
        .parse()
        .unwrap();
        let sender = SecretKey::from_slice(&b64("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw")).unwrap();
        let salt: [u8; 16] = b64("DGv6ra1nlYgDCS1FRnbzlw").try_into().unwrap();
        let body = encrypt_with(&device.keys, &sender, &salt, b"When I grow up, I want to be a watermelon");
        let expected = "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml\
                        mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT\
                        pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN";
        assert_eq!(B64.encode(&body), expected);
    }

    #[test]
    fn vapid_authorization_is_a_valid_es256_jwt_for_the_push_origin() {
        let keys = VapidKeys::from_secret(SecretKey::random(&mut OsRng));
        let url = reqwest::Url::parse("https://web.push.apple.com/abc/def?x=1").unwrap();
        let header = keys.authorization(&url, "mailto:me@example.com");
        let (token, k) = header.strip_prefix("vapid t=").unwrap().split_once(", k=").unwrap();
        assert_eq!(k, keys.public_key());
        let parts: Vec<&str> = token.split('.').collect();
        assert_eq!(parts.len(), 3);
        let claims: serde_json::Value = serde_json::from_slice(&b64(parts[1])).unwrap();
        assert_eq!(claims["aud"], "https://web.push.apple.com");
        assert_eq!(claims["sub"], "mailto:me@example.com");
        assert!(claims["exp"].as_u64().unwrap() > chrono::Utc::now().timestamp() as u64);
        let verifying = VerifyingKey::from_sec1_bytes(&b64(k)).unwrap();
        let signature = Signature::from_slice(&b64(parts[2])).unwrap();
        verifying.verify(format!("{}.{}", parts[0], parts[1]).as_bytes(), &signature).unwrap();
    }

    #[test]
    fn creates_a_private_key_once_and_reads_it_back() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sub/vapid_private.pem");
        let first = VapidKeys::load_or_create(&path).unwrap();
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert!(std::fs::read_to_string(&path).unwrap().starts_with("-----BEGIN PRIVATE KEY-----"));
        // 被放宽的权限收回
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        let again = VapidKeys::load_or_create(&path).unwrap();
        assert_eq!(first.public_key(), again.public_key());
        assert_eq!(std::fs::metadata(&path).unwrap().permissions().mode() & 0o777, 0o600);
        assert_eq!(B64.decode(first.public_key()).unwrap().len(), 65);
    }

    #[test]
    fn rejects_bad_subscriptions() {
        let good = Subscription {
            endpoint: "https://fcm.googleapis.com/fcm/send/x".into(),
            p256dh: "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4".into(),
            auth: "BTBZMqHH6r4Tts7J_aSIgg".into(),
        };
        let parsed = good.parse().unwrap();
        assert!(parsed.is_https());
        assert_eq!(parsed.host(), "fcm.googleapis.com");
        assert!(!Subscription { endpoint: "http://x/y".into(), ..good.clone() }.parse().unwrap().is_https());
        assert!(Subscription { endpoint: "not a url".into(), ..good.clone() }.parse().is_err());
        let short_auth = Subscription { auth: "AAAA".into(), ..good.clone() };
        assert!(short_auth.parse().err().unwrap().0.contains("16 bytes"));
        let bad_key = Subscription { p256dh: B64.encode([4u8; 65]), ..good };
        assert!(bad_key.parse().is_err());
    }
}
