from ..config import ProjectConfig
from .apparel import ApparelEngine
from .interior import InteriorEngine


def get_engine(cfg: ProjectConfig):
    """어패럴과 인테리어는 완전히 별도 클래스(별도 탐지 구성·판정·수정 허용 범위)."""
    return {"apparel": ApparelEngine, "interior": InteriorEngine}[cfg.work_type](cfg)
