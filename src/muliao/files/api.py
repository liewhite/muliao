"""文件领域的公开接口：别的领域只经这里使用文件领域（docs/domain-design.md 第 10 节）。文件写入后不可变、不删除，
所以在调用方的事务里读它的元数据（load_metas）没有先查后写的竞态。"""

from muliao.files.store import INLINE_IMAGE_TYPES, FileMeta, FileStore, file_url, load_metas, parse_file_url

__all__ = ["INLINE_IMAGE_TYPES", "FileMeta", "FileStore", "file_url", "load_metas", "parse_file_url"]
