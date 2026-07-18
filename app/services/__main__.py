"""服务层独立测试入口 — python -m app.services"""
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    exec(Path(__file__).parent.joinpath("__init__.py").read_text())
