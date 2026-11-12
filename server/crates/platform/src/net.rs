//! 出站 HTTP 的共用客户端：照 macOS 的系统代理设置走（本机开着 Surge），含它的例外列表（本机、局域网直连）。
//!
//! 为什么要它：本机直连不了部分境外服务（如推送到 Chrome / Android 的 fcm.googleapis.com，直连超时，#682）。Python 版的
//! requests 每次请求读系统代理，所以一直能送到。reqwest 的 `system-proxy` 特性也读系统代理，但不读例外列表，打开它连
//! 127.0.0.1 上的模型网关也会绕进代理，所以不用它，在这里读一次系统设置交给 reqwest。
//!
//! 只在启动时读一次：之后改了系统代理要重启服务才生效（罕见，不为它加监听）。没做的（罕见、本机用不到）：macOS 的
//! 「排除简单主机名」开关；`169.254/16` 这种省略写法的网段（reqwest 当成域名，匹配不上）。

/// 系统里的代理设置（HTTP、HTTPS 各一个，可能都没开）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct SystemProxy {
    /// `http://主机:端口`
    pub http: Option<String>,
    pub https: Option<String>,
    /// 不走代理的主机、IP 段（reqwest `NoProxy` 的写法，逗号分隔）
    pub exceptions: String,
}

impl SystemProxy {
    /// 读本机的系统代理设置；不是 macOS 或读不到时都没有代理。
    pub fn read() -> Self {
        #[cfg(target_os = "macos")]
        {
            mac::read()
        }
        #[cfg(not(target_os = "macos"))]
        {
            Self::default()
        }
    }

    /// 例外列表：macOS 的 `*.local` 在 reqwest 里写成 `.local`（后缀匹配），其余原样
    fn no_proxy(exceptions: &[String]) -> String {
        exceptions.iter().map(|e| e.strip_prefix('*').unwrap_or(e).trim()).collect::<Vec<_>>().join(",")
    }

    /// 把代理设置装进 reqwest 的客户端构造器。
    pub fn apply(&self, mut builder: reqwest::ClientBuilder) -> reqwest::Result<reqwest::ClientBuilder> {
        let except = || reqwest::NoProxy::from_string(&self.exceptions);
        if let Some(url) = &self.http {
            builder = builder.proxy(reqwest::Proxy::http(url)?.no_proxy(except()));
        }
        if let Some(url) = &self.https {
            builder = builder.proxy(reqwest::Proxy::https(url)?.no_proxy(except()));
        }
        Ok(builder)
    }
}

/// 各领域共用的出站客户端（模型、网页、语音、推送……由组合根建一个交给它们）：走系统代理。
pub fn client() -> reqwest::Result<reqwest::Client> {
    let proxy = SystemProxy::read();
    tracing::info!(http = ?proxy.http, https = ?proxy.https, exceptions = %proxy.exceptions, "outbound HTTP proxy from system settings");
    proxy.apply(reqwest::Client::builder())?.build()
}

#[cfg(target_os = "macos")]
mod mac {
    use system_configuration::core_foundation::array::CFArray;
    use system_configuration::core_foundation::base::{CFType, CFTypeRef, TCFType};
    use system_configuration::core_foundation::dictionary::CFDictionary;
    use system_configuration::core_foundation::number::CFNumber;
    use system_configuration::core_foundation::string::CFString;
    use system_configuration::dynamic_store::SCDynamicStoreBuilder;

    use super::SystemProxy;

    pub(super) fn read() -> SystemProxy {
        let Some(map) = SCDynamicStoreBuilder::new("muliao").build().and_then(|s| s.get_proxies()) else {
            return SystemProxy::default();
        };
        let exceptions: Vec<String> = map
            .find(CFString::new("ExceptionsList"))
            .and_then(|v| v.downcast::<CFArray>())
            .map(|list| {
                let items = list.get_all_values().into_iter();
                // SAFETY: CFArray 的元素都是 CF 对象；按 get 规则包装只加引用计数、不接管所有权，list 在这期间活着
                let items = items.map(|p| unsafe { CFType::wrap_under_get_rule(p as CFTypeRef) });
                items.filter_map(|e| e.downcast::<CFString>()).map(|s| s.to_string()).collect()
            })
            .unwrap_or_default();
        SystemProxy {
            http: proxy(&map, "HTTP"),
            https: proxy(&map, "HTTPS"),
            exceptions: SystemProxy::no_proxy(&exceptions),
        }
    }

    /// `<kind>Enable` 为 1 时的 `http://<kind>Proxy:<kind>Port`
    fn proxy(map: &CFDictionary<CFString, CFType>, kind: &str) -> Option<String> {
        let get = |key: &str| map.find(CFString::new(&format!("{kind}{key}"))).map(|v| v.as_CFType());
        let enabled = get("Enable").and_then(|v| v.downcast::<CFNumber>()).and_then(|n| n.to_i32()) == Some(1);
        let host = get("Proxy").and_then(|v| v.downcast::<CFString>()).map(|s| s.to_string())?;
        let port = get("Port").and_then(|v| v.downcast::<CFNumber>()).and_then(|n| n.to_i32())?;
        (enabled && !host.is_empty()).then(|| format!("http://{host}:{port}"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn macos_exceptions_become_reqwest_no_proxy() {
        let list = ["192.168.0.0/16", "127.0.0.1", "localhost", "*.local"].map(String::from);
        assert_eq!(SystemProxy::no_proxy(&list), "192.168.0.0/16,127.0.0.1,localhost,.local");
    }

    /// 走代理的请求发到代理，例外里的直连
    #[tokio::test]
    async fn proxies_except_the_exceptions() {
        use std::io::{Read, Write};
        // 一个记下收到什么、都答 200 的「代理」
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let (seen, requests) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            for mut conn in listener.incoming().flatten() {
                let mut buf = [0u8; 1024];
                let n = conn.read(&mut buf).unwrap_or(0);
                let _ = conn.write_all(b"HTTP/1.1 200 OK\r\ncontent-length: 0\r\nconnection: close\r\n\r\n");
                let _ = seen.send(String::from_utf8_lossy(&buf[..n]).into_owned());
            }
        });
        let proxy = SystemProxy {
            http: Some(format!("http://127.0.0.1:{port}")),
            https: None,
            exceptions: "localhost,.local".into(),
        };
        let client = proxy.apply(reqwest::Client::builder()).unwrap().build().unwrap();
        client.get("http://example.invalid/x").send().await.unwrap();
        assert!(requests.recv().unwrap().starts_with("GET http://example.invalid/x"), "sent via the proxy");
        // 例外直连：端口 1 没人听，连不上（走代理的话代理会答 200）
        assert!(client.get("http://localhost:1/").send().await.unwrap_err().is_connect());
        assert!(requests.try_recv().is_err());
    }

    #[test]
    fn reads_this_machine_without_failing() {
        let p = SystemProxy::read();
        if let Some(url) = p.https.as_ref().or(p.http.as_ref()) {
            assert!(url.starts_with("http://"), "{url}");
        }
    }
}
