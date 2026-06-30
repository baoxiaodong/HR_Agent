"""
  验证相关的工具函数
"""
import re
from typing import List, Optional
from pathlib import Path


def validate_email(email: str) -> bool:
    """Validate email address format"""
    if not email:
        return False
    
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def validate_password(password: str) -> tuple[bool, List[str]]:
    """
    Validate password strength

    Returns:
        tuple: (is_valid, list_of_errors)
    """
    errors = []

    if not password:
        errors.append("Password is required")
        return False, errors

    if len(password) < 6:
        errors.append("Password must be at least 6 characters long")

    if len(password) > 128:
        errors.append("Password must be less than 128 characters long")

    if not re.search(r'[a-z]', password):
        errors.append("Password must contain at least one lowercase letter")

    if not re.search(r'[A-Z]', password):
        errors.append("Password must contain at least one uppercase letter")

    if not re.search(r'\d', password):
        errors.append("Password must contain at least one digit")

    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        errors.append("Password must contain at least one special character")

    # Check for common weak passwords
    weak_passwords = [
        'password', '123456', 'qwerty', 'abc123', 'password123',
        'admin', 'letmein', 'welcome', 'monkey', '123456789'
    ]

    if password.lower() in weak_passwords:
        errors.append("Password is too common")

    return len(errors) == 0, errors


def validate_username(username: str) -> tuple[bool, List[str]]:
    """
    Validate username format
    
    Returns:
        tuple: (is_valid, list_of_errors)
    """
    errors = []
    
    if not username:
        errors.append("Username is required")
        return False, errors
    
    if len(username) < 3:
        errors.append("Username must be at least 3 characters long")
    
    if len(username) > 30:
        errors.append("Username must be less than 30 characters long")
    
    if not re.match(r'^[a-zA-Z0-9_-]+$', username):
        errors.append("Username can only contain letters, numbers, underscores, and hyphens")
    
    if username.startswith('_') or username.startswith('-'):
        errors.append("Username cannot start with underscore or hyphen")
    
    if username.endswith('_') or username.endswith('-'):
        errors.append("Username cannot end with underscore or hyphen")
    
    # Reserved usernames
    reserved = [
        'admin', 'administrator', 'root', 'system', 'api', 'www',
        'mail', 'email', 'support', 'help', 'info', 'contact',
        'user', 'users', 'guest', 'anonymous', 'null', 'undefined'
    ]
    
    if username.lower() in reserved:
        errors.append("Username is reserved")
    
    return len(errors) == 0, errors


def validate_file_type(filename: str, allowed_types: List[str]) -> bool:
    """Validate file type based on extension"""
    if not filename or not allowed_types:
        return False
    
    extension = Path(filename).suffix.lower()
    return extension in [ext.lower() for ext in allowed_types]


def validate_file_size(file_size: int, max_size: int) -> bool:
    """Validate file size"""
    return 0 < file_size <= max_size


def sanitize_filename(filename: str) -> str:
    """Sanitize filename for safe storage"""
    if not filename:
        return "unnamed_file"
    
    # Remove path components
    filename = Path(filename).name
    
    # Remove or replace dangerous characters
    dangerous_chars = '<>:"/\\|?*'
    for char in dangerous_chars:
        filename = filename.replace(char, '_')
    
    # Remove control characters
    filename = ''.join(char for char in filename if ord(char) >= 32)
    
    # Remove leading/trailing spaces and dots
    filename = filename.strip(' .')
    
    # Ensure filename is not empty
    if not filename:
        filename = "unnamed_file"
    
    # Limit length
    if len(filename) > 255:
        name, ext = filename.rsplit('.', 1) if '.' in filename else (filename, '')
        max_name_length = 255 - len(ext) - 1 if ext else 255
        filename = name[:max_name_length] + ('.' + ext if ext else '')
    
    return filename


def validate_phone_number(phone: str) -> bool:
    """Validate phone number format (simple validation)"""
    if not phone:
        return False
    
    # Remove all non-digit characters
    digits_only = re.sub(r'\D', '', phone)
    
    # Check if it's a valid length (10-15 digits)
    return 10 <= len(digits_only) <= 15


def validate_url(url: str) -> bool:
    """Validate URL format"""
    if not url:
        return False
    
    pattern = r'^https?://(?:[-\w.])+(?:\:[0-9]+)?(?:/(?:[\w/_.])*(?:\?(?:[\w&=%.])*)?(?:\#(?:[\w.])*)?)?$'
    return bool(re.match(pattern, url))


def validate_date_string(date_string: str, format_pattern: str = r'^\d{4}-\d{2}-\d{2}$') -> bool:
    """Validate date string format (YYYY-MM-DD by default)"""
    if not date_string:
        return False
    
    return bool(re.match(format_pattern, date_string))


def validate_uuid(uuid_string: str) -> bool:
    """Validate UUID format"""
    if not uuid_string:
        return False
    
    pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    return bool(re.match(pattern, uuid_string.lower()))


def validate_json_string(json_string: str) -> bool:
    """Validate JSON string format"""
    if not json_string:
        return False
    
    try:
        import json
        json.loads(json_string)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def validate_ip_address(ip: str) -> bool:
    """Validate IP address format (IPv4)"""
    if not ip:
        return False
    
    pattern = r'^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
    return bool(re.match(pattern, ip))


def validate_hex_color(color: str) -> bool:
    """Validate hex color format"""
    if not color:
        return False
    
    pattern = r'^#(?:[0-9a-fA-F]{3}){1,2}$'
    return bool(re.match(pattern, color))


def sanitize_html_input(text: str) -> str:
    """Sanitize HTML input to prevent XSS"""
    if not text:
        return ""
    
    import html
    
    # Escape HTML characters
    text = html.escape(text)
    
    # Remove potentially dangerous patterns
    dangerous_patterns = [
        r'javascript:',
        r'vbscript:',
        r'onload=',
        r'onerror=',
        r'onclick=',
        r'onmouseover=',
        r'<script',
        r'</script>',
        r'<iframe',
        r'</iframe>',
        r'<object',
        r'</object>',
        r'<embed',
        r'</embed>',
    ]
    
    for pattern in dangerous_patterns:
        text = re.sub(pattern, '', text, flags=re.IGNORECASE)
    
    return text


def validate_sql_input(text: str) -> bool:
    """Check for potential SQL injection patterns"""
    if not text:
        return True
    
    # Common SQL injection patterns
    sql_patterns = [
        r"'.*--",
        r'".*--',
        r"';.*--",
        r'";.*--',
        r"'.*#",
        r'".*#',
        r"union.*select",
        r"drop.*table",
        r"delete.*from",
        r"insert.*into",
        r"update.*set",
        r"exec.*\(",
        r"execute.*\(",
    ]
    
    text_lower = text.lower()
    
    for pattern in sql_patterns:
        if re.search(pattern, text_lower):
            return False
    
    return True