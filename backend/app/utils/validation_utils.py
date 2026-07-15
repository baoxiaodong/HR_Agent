"""
进入业务服务前可复用的格式校验与字符串净化工具。

这些函数只返回布尔值、错误列表或净化后的字符串，不查询数据库，也不实施权限检查。
正则校验只能作为输入质量检查：SQL 安全仍依赖参数化查询，HTML 安全应依赖与具体渲染
场景匹配的净化/转义策略，文件安全还需要业务层约束保存根目录和真实文件内容。
"""
import re
from typing import List, Optional
from pathlib import Path


def validate_email(email: str) -> bool:
    """用轻量正则检查邮箱地址外形，不验证域名或邮箱是否真实存在。"""
    if not email:
        return False
    
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def validate_password(password: str) -> tuple[bool, List[str]]:
    """按当前密码策略返回 ``(是否通过, 全部错误)``。

    该函数检查长度、字符种类和少量常见弱密码，不负责哈希、泄露密码库比对或登录限流；
    通过校验的明文仍必须交给安全模块哈希后才能持久化。
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
    """按长度、允许字符和保留字规则返回用户名校验结果及全部错误。"""
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
    """按扩展名检查文件类型，不读取或验证文件真实内容。"""
    if not filename or not allowed_types:
        return False
    
    extension = Path(filename).suffix.lower()
    return extension in [ext.lower() for ext in allowed_types]


def validate_file_size(file_size: int, max_size: int) -> bool:
    """检查文件字节数处于 ``(0, max_size]`` 范围。"""
    return 0 < file_size <= max_size


def sanitize_filename(filename: str) -> str:
    """去掉目录部分、控制字符和常见非法字符，得到单个文件名。

    结果只适合作为保存路径的一个组成部分；调用方仍需在可信根目录下拼接并校验最终路径，
    同时处理同名文件和扩展名伪装。
    """
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
    """去掉非数字字符后，按 10 到 15 位长度做宽松校验。"""
    if not phone:
        return False
    
    # Remove all non-digit characters
    digits_only = re.sub(r'\D', '', phone)
    
    # Check if it's a valid length (10-15 digits)
    return 10 <= len(digits_only) <= 15


def validate_url(url: str) -> bool:
    """按当前正则检查 HTTP(S) URL 外形，不发起网络连接或判断目标可信性。"""
    if not url:
        return False
    
    pattern = r'^https?://(?:[-\w.])+(?:\:[0-9]+)?(?:/(?:[\w/_.])*(?:\?(?:[\w&=%.])*)?(?:\#(?:[\w.])*)?)?$'
    return bool(re.match(pattern, url))


def validate_date_string(date_string: str, format_pattern: str = r'^\d{4}-\d{2}-\d{2}$') -> bool:
    """检查日期字符串外形；默认不验证月份和日期是否真实存在。"""
    if not date_string:
        return False
    
    return bool(re.match(format_pattern, date_string))


def validate_uuid(uuid_string: str) -> bool:
    """检查标准连字符 UUID 文本及其版本、variant 位。"""
    if not uuid_string:
        return False
    
    pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    return bool(re.match(pattern, uuid_string.lower()))


def validate_json_string(json_string: str) -> bool:
    """确认字符串可被标准库解析为任意合法 JSON 值。"""
    if not json_string:
        return False
    
    try:
        import json
        json.loads(json_string)
        return True
    except (json.JSONDecodeError, TypeError):
        return False


def validate_ip_address(ip: str) -> bool:
    """按四个十进制分段检查 IPv4 文本。"""
    if not ip:
        return False
    
    pattern = r'^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
    return bool(re.match(pattern, ip))


def validate_hex_color(color: str) -> bool:
    """检查三位或六位十六进制颜色文本。"""
    if not color:
        return False
    
    pattern = r'^#(?:[0-9a-fA-F]{3}){1,2}$'
    return bool(re.match(pattern, color))


def sanitize_html_input(text: str) -> str:
    """转义 HTML 字符，并移除少量已知危险协议/事件字符串。

    ``html.escape`` 已把标签转换成文本，后续正则仅是附加过滤。此函数适合纯文本展示；若
    产品需要保留部分 HTML，应改用带允许列表的专业净化器，而不是依赖这里的模式集合。
    """
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
    """拒绝少量常见 SQL 注入外形，仅作为提示性预检。

    正则黑名单既可能误伤正常文本，也无法覆盖编码、注释和数据库方言变体；数据库访问必须
    始终使用绑定参数，不能因本函数返回 ``True`` 就拼接 SQL。
    """
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