"""
文件内容、上传落盘和本地路径操作工具。

上传文件先完整读入内存，再检查大小、计算 SHA-256 并写入调用方给定的目标路径；数据库
记录和向量库写入不在本模块职责内，因此跨存储的一致性必须由业务服务编排。文件扩展名和
MIME 推断都只是格式提示，不等同于验证文件真实内容。
"""
import hashlib
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Optional, BinaryIO
import logging

from fastapi import UploadFile

logger = logging.getLogger(__name__)


def get_file_hash(file_content: bytes) -> str:
    """计算文件内容的 SHA-256，用于内容去重或完整性比较。"""
    return hashlib.sha256(file_content).hexdigest()


def get_file_mime_type(filename: str) -> str:
    """根据文件名扩展名推断 MIME；未知扩展名返回通用二进制类型。"""
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"


def ensure_directory(directory: Path) -> None:
    """递归创建目录；目录已存在时保持不变。"""
    directory.mkdir(parents=True, exist_ok=True)


async def save_uploaded_file(
    upload_file: UploadFile,
    destination: Path,
    max_size: int = 10 * 1024 * 1024  # 10MB default
) -> tuple[str, int]:
    """把 FastAPI 上传对象保存到指定路径，返回内容哈希和字节数。

    ``destination`` 必须由调用方根据可信根目录构造。本函数会创建父目录，但不会校验
    路径是否越界；超过 ``max_size`` 时在任何文件写入前抛出 ``ValueError``。
    """
    # Ensure destination directory exists
    ensure_directory(destination.parent)
    
    # Read file content
    content = await upload_file.read()
    file_size = len(content)
    
    # Check file size
    if file_size > max_size:
        raise ValueError(f"File size {file_size} exceeds maximum {max_size}")
    
    # Calculate hash
    file_hash = get_file_hash(content)
    
    # Save file
    with open(destination, "wb") as f:
        f.write(content)
    
    logger.info(f"File saved: {destination} (size: {file_size}, hash: {file_hash})")
    
    return file_hash, file_size


def delete_file_safe(file_path: Path) -> bool:
    """尝试删除文件；不存在或删除失败均返回 ``False``，不向上抛出。"""
    try:
        if file_path.exists():
            file_path.unlink()
            logger.info(f"File deleted: {file_path}")
            return True
        else:
            logger.warning(f"File not found for deletion: {file_path}")
            return False
    except Exception as e:
        logger.error(f"Error deleting file {file_path}: {e}")
        return False


def copy_file_safe(source: Path, destination: Path) -> bool:
    """复制文件并保留元数据；失败时记录日志并返回 ``False``。"""
    try:
        ensure_directory(destination.parent)
        shutil.copy2(source, destination)
        logger.info(f"File copied: {source} -> {destination}")
        return True
    except Exception as e:
        logger.error(f"Error copying file {source} to {destination}: {e}")
        return False


def get_file_size(file_path: Path) -> int:
    """读取文件字节数；路径不存在或无法访问时返回 0。"""
    try:
        return file_path.stat().st_size
    except Exception:
        return 0


def is_file_type_allowed(filename: str, allowed_types: list[str]) -> bool:
    """按小写扩展名判断是否在允许列表中，不验证真实内容。"""
    if not filename:
        return False
    
    extension = Path(filename).suffix.lower()
    return extension in [ext.lower() for ext in allowed_types]


def sanitize_filename(filename: str) -> str:
    """替换常见非法字符并限制文件名长度。

    与 ``validation_utils.sanitize_filename`` 不同，此函数不会先移除目录部分或控制字符；
    调用方不能把未经校验的用户路径直接传入。
    """
    # Remove or replace dangerous characters
    dangerous_chars = '<>:"/\\|?*'
    for char in dangerous_chars:
        filename = filename.replace(char, '_')
    
    # Remove leading/trailing spaces and dots
    filename = filename.strip(' .')
    
    # Ensure filename is not empty
    if not filename:
        filename = "unnamed_file"
    
    # Limit length
    if len(filename) > 255:
        name, ext = os.path.splitext(filename)
        filename = name[:255-len(ext)] + ext
    
    return filename


def get_unique_filename(directory: Path, filename: str) -> str:
    """文件名已存在时追加递增序号。

    检查与后续写入不是原子操作，并发上传仍需由调用方处理竞争。
    """
    base_path = directory / filename
    
    if not base_path.exists():
        return filename
    
    name, ext = os.path.splitext(filename)
    counter = 1
    
    while True:
        new_filename = f"{name}_{counter}{ext}"
        new_path = directory / new_filename
        
        if not new_path.exists():
            return new_filename
        
        counter += 1


class FileManager:
    """围绕一个基础目录提供简单的二进制文件读写门面。

    ``relative_path`` 当前直接与基础目录拼接，没有调用 ``resolve`` 后验证归属；因此只应
    接收业务层生成或已严格净化的相对路径，不能直接接收用户提供的 ``..`` 或绝对路径。
    文件操作也不会与数据库事务自动提交或回滚。
    """
    
    def __init__(self, base_directory: Path):
        self.base_directory = Path(base_directory)
        ensure_directory(self.base_directory)
    
    def get_file_path(self, relative_path: str) -> Path:
        """把相对路径与基础目录拼接，不自动校验越界。"""
        return self.base_directory / relative_path
    
    def save_file(self, content: bytes, relative_path: str) -> Path:
        """创建父目录并把二进制内容写入相对路径。"""
        file_path = self.get_file_path(relative_path)
        ensure_directory(file_path.parent)
        
        with open(file_path, "wb") as f:
            f.write(content)
        
        return file_path
    
    def read_file(self, relative_path: str) -> bytes:
        """以二进制方式读取相对路径，I/O 异常直接向上传播。"""
        file_path = self.get_file_path(relative_path)
        
        with open(file_path, "rb") as f:
            return f.read()
    
    def delete_file(self, relative_path: str) -> bool:
        """删除相对路径文件，并沿用 ``delete_file_safe`` 的布尔降级语义。"""
        file_path = self.get_file_path(relative_path)
        return delete_file_safe(file_path)
    
    def file_exists(self, relative_path: str) -> bool:
        """检查拼接后的路径是否存在。"""
        file_path = self.get_file_path(relative_path)
        return file_path.exists()
    
    def list_files(self, relative_directory: str = "") -> list[str]:
        """列出目录第一层普通文件，并返回相对基础目录的排序路径。"""
        directory = self.get_file_path(relative_directory)
        
        if not directory.exists() or not directory.is_dir():
            return []
        
        files = []
        for item in directory.iterdir():
            if item.is_file():
                files.append(str(item.relative_to(self.base_directory)))
        
        return sorted(files)