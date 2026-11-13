use super::*;
use tokio::io::AsyncWriteExt;

async fn store(max_bytes: u64) -> (tempfile::TempDir, FileStore) {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("t.db");
    muliao_platform::db::migrate(&db_path, &[MIGRATION]).unwrap();
    let db = Arc::new(Database::open(&db_path, Arc::new(|| {})).unwrap());
    let root = dir.path().join("files");
    (dir, FileStore::open(db, root, max_bytes).unwrap())
}

fn tmp_count(s: &FileStore) -> usize {
    std::fs::read_dir(s.tmp_dir()).map(|d| d.count()).unwrap_or(0)
}

#[tokio::test(flavor = "multi_thread")]
async fn put_dedups_content_and_keeps_metadata_per_upload() {
    let (_dir, s) = store(1024).await;
    let a = s.put("a.txt", "Text/Plain; charset=utf-8", &b"hello"[..]).await.unwrap();
    let b = s.put("b.txt", "text/plain", &b"hello"[..]).await.unwrap();
    assert_ne!(a.id, b.id);
    assert_eq!(a.sha256, b.sha256);
    assert_eq!(a.sha256, "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
    assert_eq!(a.media_type, "text/plain");
    assert_eq!(a.size, 5);
    assert_eq!(s.blob_path(&a), s.blob_path(&b));
    assert_eq!(s.read(&b).await.unwrap(), b"hello");
    assert_eq!(tmp_count(&s), 0);

    let got = s.get_many(vec![b.id.clone(), a.id.clone()]).await.unwrap();
    assert_eq!(got, vec![b.clone(), a.clone()], "in the order asked, round-tripped through the database");
    assert_eq!(s.get(&a.id).await.unwrap().name, "a.txt");
}

#[tokio::test(flavor = "multi_thread")]
async fn missing_ids_are_not_found_for_callers_and_corrupt_for_stored_references() {
    let (_dir, s) = store(1024).await;
    let a = s.put("a.png", "image/png", &b"x"[..]).await.unwrap();
    let err = s.get_many(vec![a.id.clone(), "nope".into()]).await.unwrap_err();
    assert!(matches!(err, Error::Domain(ref e) if e.kind == ErrorKind::NotFound), "{err}");
    let err = s.referenced(vec![a.id.clone(), "nope".into()]).await.unwrap_err();
    assert!(matches!(err, Error::Internal(_)) && err.to_string().contains("nope"), "{err}");
    assert!(a.is_inline_image());
}

#[tokio::test(flavor = "multi_thread")]
async fn too_large_stops_and_leaves_nothing() {
    let (_dir, s) = store(4).await;
    let err = s.put("big", "application/octet-stream", &b"12345"[..]).await.unwrap_err();
    assert!(matches!(err, Error::Domain(ref e) if e.kind == ErrorKind::TooLarge), "{err}");
    assert_eq!(tmp_count(&s), 0);
    let rows: i64 = s.db.read(|c| c.query_row("SELECT count(*) FROM files", [], |r| r.get(0))).await.unwrap();
    assert_eq!(rows, 0);
    // 正好等于上限可以
    s.put("ok", "application/octet-stream", &b"1234"[..]).await.unwrap();
}

#[tokio::test(flavor = "multi_thread")]
async fn bad_names_and_types_are_rejected_before_reading() {
    let (_dir, s) = store(1024).await;
    for name in ["", ".", "..", "a/b", "a\\b", "a\nb", &"x".repeat(256)] {
        let err = s.put(name, "text/plain", &b"x"[..]).await.unwrap_err();
        assert!(matches!(err, Error::Domain(ref e) if e.kind == ErrorKind::InvalidInput), "{name:?}");
    }
    for t in ["text", "/plain", "text/", "te xt/plain", ""] {
        assert!(check_media_type(t).is_err(), "{t:?}");
    }
    assert!(check_name(&"中".repeat(85)).is_ok(), "255 bytes");
    assert!(check_name(&"中".repeat(86)).is_err(), "258 bytes");
}

#[tokio::test(flavor = "multi_thread")]
async fn open_removes_leftover_temp_files() {
    let (_dir, s) = store(1024).await;
    std::fs::create_dir_all(s.tmp_dir()).unwrap();
    std::fs::write(s.tmp_dir().join("half"), b"x").unwrap();
    let s = FileStore::open(s.db.clone(), s.root.clone(), 1024).unwrap();
    assert_eq!(tmp_count(&s), 0);
}

#[tokio::test(flavor = "multi_thread")]
async fn cancelled_upload_leaves_no_temp_file() {
    let (_dir, s) = store(1024).await;
    let s = Arc::new(s);
    // 先给几个字节，然后一直不结束（客户端上传到一半）
    let (mut writer, reader) = tokio::io::duplex(64);
    writer.write_all(b"half").await.unwrap();
    let st = s.clone();
    let upload = tokio::spawn(async move { st.put("a", "text/plain", reader).await });
    while tmp_count(&s) == 0 {
        tokio::time::sleep(std::time::Duration::from_millis(5)).await;
    }
    upload.abort();
    assert!(upload.await.unwrap_err().is_cancelled());
    assert_eq!(tmp_count(&s), 0);
    drop(writer);
}

#[test]
fn file_url_round_trips() {
    let url = file_url("abc", "报告 v1/2?.pdf");
    assert_eq!(url, "/api/files/abc/%E6%8A%A5%E5%91%8A%20v1%2F2%3F.pdf");
    assert_eq!(parse_file_url(&url), Some(("abc".into(), "报告 v1/2?.pdf".into())));
    assert_eq!(file_url("i", "a-b_c.~d"), "/api/files/i/a-b_c.~d");
    for bad in ["/api/files/abc", "/api/files//x", "/api/filesx/a/b", "/other/a/b", "/api/files/a/"] {
        assert_eq!(parse_file_url(bad), None, "{bad}");
    }
}
