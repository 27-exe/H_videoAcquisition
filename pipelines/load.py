from pathlib import Path
import json
import yaml
import logging

logger = logging.getLogger(__name__)


CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"   # 根据实际情况调整 1→0/2/3

def load_json(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"JSON 解析失败 {path}: {e}")
        raise
    except Exception as e:
        logger.error(f"读取失败 {path}: {e}", exc_info=True)
        raise



def load_yaml(filename: str) -> dict:
    path = CONFIG_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")
    if not path.is_file():
        raise ValueError(f"路径不是文件：{path}")

    try:
        with path.open("r", encoding="utf-8") as f:
            content = yaml.safe_load(f)
        return content if isinstance(content, dict) else {}
    except yaml.YAMLError as e:
        logger.error(f"无效的 YAML 格式 {path}: {e}")
        raise


def save_to_yaml(data: list, filename: str):
    """
    将列表数据保存为 YAML 文件
    """
    path = CONFIG_DIR / filename

    try:
        # 确保父级目录存在
        path.parent.mkdir(parents=True, exist_ok=True)

        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

        print(f"成功保存到：{path}")
    except Exception as e:
        logger.error(f"写入 YAML 失败 {path}: {e}")
        raise