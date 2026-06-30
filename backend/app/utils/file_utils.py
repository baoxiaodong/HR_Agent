"""
    文件工具函数
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
    """Generate SHA-256 hash of file content"""
    return hashlib.sha256(file_content).hexdigest()


def get_file_mime_type(filename: str) -> str:
    """Get MIME type of file based on extension"""
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"


def ensure_directory(directory: Path) -> None:
    """Ensure directory exists, create if it doesn't"""
    directory.mkdir(parents=True, exist_ok=True)


async def save_uploaded_file(
    upload_file: UploadFile,
    destination: Path,
    max_size: int = 10 * 1024 * 1024  # 10MB default
) -> tuple[str, int]:
    """
    Save uploaded file to destination
    
    Returns:
        tuple: (file_hash, file_size)
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
    """
    Safely delete file, return True if successful
    """
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
    """
    Safely copy file, return True if successful
    """
    try:
        ensure_directory(destination.parent)
        shutil.copy2(source, destination)
        logger.info(f"File copied: {source} -> {destination}")
        return True
    except Exception as e:
        logger.error(f"Error copying file {source} to {destination}: {e}")
        return False


def get_file_size(file_path: Path) -> int:
    """Get file size in bytes"""
    try:
        return file_path.stat().st_size
    except Exception:
        return 0


def is_file_type_allowed(filename: str, allowed_types: list[str]) -> bool:
    """Check if file type is allowed based on extension"""
    if not filename:
        return False
    
    extension = Path(filename).suffix.lower()
    return extension in [ext.lower() for ext in allowed_types]


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe storage"""
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
    """Get unique filename in directory by adding counter if needed"""
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
    """File management utility class"""
    
    def __init__(self, base_directory: Path):
        self.base_directory = Path(base_directory)
        ensure_directory(self.base_directory)
    
    def get_file_path(self, relative_path: str) -> Path:
        """Get absolute file path from relative path"""
        return self.base_directory / relative_path
    
    def save_file(self, content: bytes, relative_path: str) -> Path:
        """Save file content to relative path"""
        file_path = self.get_file_path(relative_path)
        ensure_directory(file_path.parent)
        
        with open(file_path, "wb") as f:
            f.write(content)
        
        return file_path
    
    def read_file(self, relative_path: str) -> bytes:
        """Read file content from relative path"""
        file_path = self.get_file_path(relative_path)
        
        with open(file_path, "rb") as f:
            return f.read()
    
    def delete_file(self, relative_path: str) -> bool:
        """Delete file at relative path"""
        file_path = self.get_file_path(relative_path)
        return delete_file_safe(file_path)
    
    def file_exists(self, relative_path: str) -> bool:
        """Check if file exists at relative path"""
        file_path = self.get_file_path(relative_path)
        return file_path.exists()
    
    def list_files(self, relative_directory: str = "") -> list[str]:
        """List files in relative directory"""
        directory = self.get_file_path(relative_directory)
        
        if not directory.exists() or not directory.is_dir():
            return []
        
        files = []
        for item in directory.iterdir():
            if item.is_file():
                files.append(str(item.relative_to(self.base_directory)))
        
        return sorted(files)