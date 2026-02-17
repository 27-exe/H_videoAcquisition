import logging,re

logger = logging.getLogger(__name__)


FORBIDDEN_RE = re.compile(r'[\x00-\x1F\x7F<>:"/\\|?*]')
WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "COM5",
    "COM6", "COM7", "COM8", "COM9", "LPT1", "LPT2", "LPT3", "LPT4",
    "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}


def clean_filename(name: str) -> str:
    if not name:
        return "未命名文件"

    #  替换非法字符
    cleaned = FORBIDDEN_RE.sub('_', name).strip()
    cleaned = cleaned.rstrip('.')
    # 处理空字符串或全是空格的情况
    if not cleaned or cleaned.isspace():
        return "未命名文件"
    #  检查 Windows 保留字
    main_name = cleaned.split('.')[0].upper()
    if main_name in WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:200]

def make_result(urls,results):
    processed = []
    for url, result in zip(urls, results):
        if url == 0 or result == 0:
            processed.append(0)
        elif isinstance(result, Exception):
            processed.append({
                "status": "error",
                "content": str(result) + type(result).__name__
            })
        else:
            processed.append(  {
                "status": "success",
                "content": result
            })
    return processed