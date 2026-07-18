"""策略模块独立测试入口 — python -m app.strategy"""
if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent.parent))
    # 运行 Edge Score 完整测试
    exec(Path(__file__).parent.joinpath("edge.py").read_text())
