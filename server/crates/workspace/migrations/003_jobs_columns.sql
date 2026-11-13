-- #803：jobs 的 secrets（注入了哪些 vault key，没有人读）与 log_path（总是 jobs/<id>.log，由 jobs::log_path 算出；上线前
-- 核对线上 471 行全是这个形式）删掉
ALTER TABLE jobs DROP COLUMN secrets;
ALTER TABLE jobs DROP COLUMN log_path;
