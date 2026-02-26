"""
通用日志轮转工具
支持追加到现有压缩包，更新日期
"""

import shutil
import zipfile
from datetime import datetime
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

def rotate_logs(log_dir, log_name, max_lines=5000, max_backups=3, 
                archive_name_prefix="bot_log_archive", compression_level=4):
    """
    轮转日志文件，将旧日志压缩到带日期的归档中
    
    参数:
        log_dir: 日志目录 (Path 或 str)
        log_name: 日志文件名 (如 "bot.log")
        max_lines: 触发轮转的最大行数
        max_backups: 保留的未压缩备份数量
        archive_name_prefix: 压缩包前缀
        compression_level: 压缩级别 (0-9)
    """
    log_dir = Path(log_dir)
    log_file = log_dir / log_name
    
    if not log_file.exists():
        return
    
    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        logger.warning(f"读取日志文件失败: {e}")
        return
    
    if len(lines) <= max_lines:
        return
    
    today = datetime.now().strftime("%Y%m%d")
    archive_name = f"{archive_name_prefix}_{today}.zip"
    archive_file = log_dir / archive_name
    tmp_dir = log_dir / f"tmp_{today}"
    
    base_name = log_name.replace('.log', '')
    existing_backups = []
    for i in range(1, max_backups + 1):
        backup_file = log_dir / f"{base_name}.{i}.log"
        if backup_file.exists():
            existing_backups.append(backup_file)
    
    if existing_backups:
        try:
            if archive_file.exists():
                _update_archive(archive_file, tmp_dir, existing_backups, compression_level)
            else:
                _create_archive(archive_file, existing_backups, compression_level)
        except Exception as e:
            logger.warning(f"压缩备份文件失败: {e}")
    
    try:
        for i in range(max_backups, 0, -1):
            src = log_dir / f"{base_name}.{i}.log"
            dst = log_dir / f"{base_name}.{i+1}.log"
            if src.exists():
                shutil.move(str(src), str(dst))
    except Exception as e:
        logger.warning(f"移动备份文件失败: {e}")
    
    try:
        shutil.move(str(log_file), str(log_dir / f"{base_name}.1.log"))
    except Exception as e:
        logger.warning(f"移动日志文件失败: {e}")
    
    try:
        log_file.touch()
    except Exception as e:
        logger.warning(f"创建新日志文件失败: {e}")


def _create_archive(archive_file, files, compression_level):
    """创建新的压缩包"""
    with zipfile.ZipFile(archive_file, 'w', zipfile.ZIP_DEFLATED, 
                        compresslevel=compression_level) as zf:
        for file in files:
            zf.write(file, file.name)
            file.unlink()


def _update_archive(archive_file, tmp_dir, new_files, compression_level):
    """更新现有压缩包"""
    tmp_dir.mkdir(exist_ok=True)
    
    with zipfile.ZipFile(archive_file, 'r') as zf:
        zf.extractall(tmp_dir)
    
    for file in new_files:
        shutil.copy2(str(file), str(tmp_dir / file.name))
    
    with zipfile.ZipFile(archive_file, 'w', zipfile.ZIP_DEFLATED,
                        compresslevel=compression_level) as zf:
        for file in tmp_dir.iterdir():
            if file.is_file():
                zf.write(file, file.name)
    
    shutil.rmtree(tmp_dir)
    
    for file in new_files:
        if file.exists():
            file.unlink()


def main():
    """手动触发轮转"""
    log_dir = Path(__file__).parent / "log"
    rotate_logs(log_dir, "bot.log", max_lines=5000, max_backups=3,
                archive_name_prefix="bot_log_archive", compression_level=4)
    print("日志轮转完成。")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
