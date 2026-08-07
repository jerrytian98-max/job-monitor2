"""
职位数据库管理模块
使用SQLite存储职位信息，实现持久化
"""

import sqlite3
from datetime import datetime
from typing import List, Dict, Optional
import logging
import os

from job_identity import canonicalize_job_url, job_identity_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.environ.get('JOB_PROFILE', '')
PROFILE_SUFFIX = f'_{PROFILE}' if PROFILE else ''
DB_FILE = os.path.join(BASE_DIR, f'jobs{PROFILE_SUFFIX}.db')


logger = logging.getLogger(__name__)


class JobDatabase:
    """职位数据库管理器"""
    
    def __init__(self, db_path: str = DB_FILE):
        """
        初始化数据库
        
        Args:
            db_path: 数据库文件路径
        """
        self.db_path = db_path
        self._init_db()
    
    def _get_connection(self) -> sqlite3.Connection:
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_db(self):
        """初始化数据库表结构"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # 创建职位表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    salary TEXT,
                    city TEXT,
                    description TEXT,
                    url TEXT NOT NULL,
                    source_site TEXT,
                    site_label TEXT,
                    publish_time TEXT,
                    found_time TEXT DEFAULT CURRENT_TIMESTAMP,
                    is_notified BOOLEAN DEFAULT 0,
                    job_hash TEXT UNIQUE
                )
            ''')

            # 兼容旧数据库：已有 jobs 表时补充网址标签字段。
            cursor.execute('PRAGMA table_info(jobs)')
            columns = {row['name'] for row in cursor.fetchall()}
            if 'site_label' not in columns:
                cursor.execute('ALTER TABLE jobs ADD COLUMN site_label TEXT')
            
            # 创建索引
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_company ON jobs(company)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_found_time ON jobs(found_time)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_is_notified ON jobs(is_notified)
            ''')
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_site_label ON jobs(site_label)
            ''')
            
            conn.commit()
            removed_duplicates, backup_path = self._migrate_job_identities(conn)
            if removed_duplicates:
                logger.info(
                    "已合并 %s 条因追踪参数产生的重复职位，备份文件: %s",
                    removed_duplicates,
                    backup_path,
                )
            logger.info("数据库初始化成功")
            
        except Exception as e:
            logger.error(f"初始化数据库失败: {e}")
            conn.rollback()
        finally:
            conn.close()
    
    def _migrate_job_identities(self, conn: sqlite3.Connection) -> tuple[int, str]:
        """Upgrade legacy hashes and merge rows that only differ by tracking IDs."""
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT id, title, company, url, description, site_label,
                   is_notified, job_hash
            FROM jobs
            ORDER BY datetime(found_time) ASC, id ASC
            '''
        )
        groups = {}
        for row in cursor.fetchall():
            record = dict(row)
            identity_hash = self._generate_hash(record)
            groups.setdefault(identity_hash, []).append(record)

        duplicate_groups = [rows for rows in groups.values() if len(rows) > 1]
        backup_path = ""
        if duplicate_groups:
            backup_path = self._backup_before_deduplication(conn)
            if not backup_path:
                raise RuntimeError("数据库备份失败，已取消重复职位清理")

        removed_duplicates = 0
        for identity_hash, rows in groups.items():
            winner = rows[0]
            canonical_url = canonicalize_job_url(winner.get("url"))
            loser_ids = [row["id"] for row in rows[1:]]
            if loser_ids:
                placeholders = ", ".join("?" for _ in loser_ids)
                cursor.execute(
                    f"DELETE FROM jobs WHERE id IN ({placeholders})",
                    loser_ids,
                )
                removed_duplicates += cursor.rowcount

            merged_label = self._merge_site_labels(rows)
            is_notified = int(any(bool(row.get("is_notified")) for row in rows))
            cursor.execute(
                '''
                UPDATE jobs
                SET url = ?, job_hash = ?, site_label = ?, is_notified = ?
                WHERE id = ?
                ''',
                (
                    canonical_url or winner.get("url", ""),
                    identity_hash,
                    merged_label,
                    is_notified,
                    winner["id"],
                ),
            )

        conn.commit()
        return removed_duplicates, backup_path

    @staticmethod
    def _merge_site_labels(rows: List[Dict]) -> str:
        labels = []
        for row in rows:
            for label in str(row.get("site_label") or "").split(" / "):
                label = label.strip()
                if label and label not in labels:
                    labels.append(label)
        return " / ".join(labels)

    def _backup_before_deduplication(self, conn: sqlite3.Connection) -> str:
        backup_path = (
            f"{self.db_path}.dedup-backup-"
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )
        backup_conn = None
        try:
            backup_conn = sqlite3.connect(backup_path)
            conn.backup(backup_conn)
            return backup_path
        except Exception as exc:
            logger.error("清理重复职位前备份数据库失败: %s", exc)
            return ""
        finally:
            if backup_conn is not None:
                backup_conn.close()

    def _generate_hash(self, job: Dict) -> str:
        """生成职位的唯一哈希值（主要基于URL和标题，避免因时间等可变因素导致重复）"""
        return job_identity_hash(job)
    
    def add_job(self, job: Dict) -> bool:
        """
        添加职位到数据库
        
        Args:
            job: 职位信息字典
        
        Returns:
            True表示添加成功，False表示职位已存在或添加失败
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            job_hash = self._generate_hash(job)
            
            # 检查是否已存在
            cursor.execute('SELECT id FROM jobs WHERE job_hash = ?', (job_hash,))
            if cursor.fetchone():
                return False
            
            # 插入新职位
            cursor.execute('''
                INSERT INTO jobs (
                    title, company, salary, city, description, url,
                    source_site, site_label, publish_time, found_time, job_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                job.get('title', ''),
                job.get('company', ''),
                job.get('salary', ''),
                job.get('city', ''),
                job.get('description', ''),
                job.get('url', ''),
                job.get('source_site', ''),
                job.get('site_label', ''),
                str(job.get('publish_time') or '').strip() or '未知',
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                job_hash
            ))
            
            conn.commit()
            logger.info(
                "职位已添加到数据库: %s - %s",
                job.get('title', '未知职位'),
                job.get('company', '未知公司')
            )
            return True
            
        except Exception as e:
            logger.error(f"添加职位失败: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()
    
    def job_exists(self, job: Dict) -> bool:
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            job_hash = self._generate_hash(job)
            cursor.execute('SELECT id FROM jobs WHERE job_hash = ?', (job_hash,))
            return cursor.fetchone() is not None
        finally:
            conn.close()

    def get_job_state(self, job: Dict) -> Optional[Dict]:
        """返回职位的数据库记录；职位不存在时返回 ``None``。"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT id, is_notified, found_time, site_label FROM jobs WHERE job_hash = ?',
                (self._generate_hash(job),)
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def update_job_site_label(self, job: Dict) -> bool:
        """Refresh the searchable label of an existing deduplicated job."""
        site_label = str(job.get('site_label') or '').strip()
        if not site_label:
            return False

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE jobs
                SET site_label = ?
                WHERE job_hash = ?
                  AND COALESCE(site_label, '') <> ?
                ''',
                (site_label, self._generate_hash(job), site_label),
            )
            conn.commit()
            return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"更新职位网址标签失败: {e}")
            return False
        finally:
            conn.close()

    def get_job(self, job_id: int) -> Optional[Dict]:
        """按数据库 ID 获取单个职位。"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM jobs WHERE id = ?', (job_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def add_jobs_batch(self, jobs: List[Dict]) -> int:
        """
        批量添加职位
        
        Args:
            jobs: 职位列表
        
        Returns:
            实际添加的职位数量
        """
        added_count = 0
        for job in jobs:
            if self.add_job(job):
                added_count += 1
        return added_count
    
    def get_all_jobs(
        self,
        limit: Optional[int] = None,
        offset: Optional[int] = 0,
        site_label_keyword: str = '',
    ) -> List[Dict]:
        """
        获取所有职位
        
        Args:
            limit: 限制数量
            offset: 偏移量
        
        Returns:
            职位列表
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            query = 'SELECT * FROM jobs'
            params = []
            if site_label_keyword:
                query += (
                    " WHERE COALESCE(NULLIF(site_label, ''), source_site, '') LIKE ?"
                )
                params.append(f'%{site_label_keyword}%')
            query += '''
                ORDER BY
                    COALESCE(
                        datetime(NULLIF(TRIM(publish_time), '未知')),
                        datetime(found_time)
                    ) DESC,
                    datetime(found_time) DESC,
                    id DESC
            '''
            if limit:
                query += ' LIMIT ? OFFSET ?'
                params.extend([limit, offset])
            cursor.execute(query, params)
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except Exception as e:
            logger.error(f"获取职位失败: {e}")
            return []
        finally:
            conn.close()
    
    def get_new_jobs(self, hours: int = 24) -> List[Dict]:
        """
        获取最近的新职位
        
        Args:
            hours: 最近多少小时内的职位
        
        Returns:
            职位列表
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT * FROM jobs 
                WHERE found_time >= datetime('now', '-' || ? || ' hours')
                ORDER BY found_time DESC
            ''', (hours,))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except Exception as e:
            logger.error(f"获取新职位失败: {e}")
            return []
        finally:
            conn.close()
    
    def get_jobs_by_company(self, company: str) -> List[Dict]:
        """
        获取指定公司的职位
        
        Args:
            company: 公司名称
        
        Returns:
            职位列表
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT * FROM jobs 
                WHERE company LIKE ?
                ORDER BY found_time DESC
            ''', (f'%{company}%',))
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except Exception as e:
            logger.error(f"获取公司职位失败: {e}")
            return []
        finally:
            conn.close()
    
    def mark_as_notified(self, job_ids: List[int]) -> bool:
        """
        标记职位已通知
        
        Args:
            job_ids: 职位ID列表
        
        Returns:
            True表示成功
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            for job_id in job_ids:
                cursor.execute('UPDATE jobs SET is_notified = 1 WHERE id = ?', (job_id,))
            
            conn.commit()
            return True
            
        except Exception as e:
            logger.error(f"标记已通知失败: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def mark_jobs_as_notified(self, jobs: List[Dict]) -> bool:
        """按职位内容批量标记通知状态。"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            hashes = [(self._generate_hash(job),) for job in jobs]
            cursor.executemany(
                'UPDATE jobs SET is_notified = 1 WHERE job_hash = ?',
                hashes
            )
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"标记职位已通知失败: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()
    
    def get_statistics(self) -> Dict:
        """
        获取统计信息
        
        Returns:
            统计数据字典
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            # 总职位数
            cursor.execute('SELECT COUNT(*) as total FROM jobs')
            total = cursor.fetchone()['total']
            
            # 今日新增
            cursor.execute('''
                SELECT COUNT(*) as today 
                FROM jobs 
                WHERE DATE(found_time) = DATE('now', 'localtime')
            ''')
            today = cursor.fetchone()['today']
            
            # 未通知
            cursor.execute('SELECT COUNT(*) as not_notified FROM jobs WHERE is_notified = 0')
            not_notified = cursor.fetchone()['not_notified']
            
            # 公司分布
            cursor.execute('''
                SELECT company, COUNT(*) as count 
                FROM jobs 
                GROUP BY company 
                ORDER BY count DESC 
                LIMIT 10
            ''')
            top_companies = [dict(row) for row in cursor.fetchall()]
            
            return {
                'total_jobs': total,
                'new_jobs_today': today,
                'jobs_not_notified': not_notified,
                'top_companies': top_companies
            }
            
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {}
        finally:
            conn.close()
    
    def clear_old_jobs(self, days: int = 30) -> int:
        """
        清除旧职位
        
        Args:
            days: 保留最近多少天的职位
        
        Returns:
            删除的职位数量
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            cursor.execute('''
                DELETE FROM jobs 
                WHERE found_time < datetime('now', '-' || ? || ' days')
            ''', (days,))
            
            deleted_count = cursor.rowcount
            conn.commit()
            
            logger.info(f"已清除 {deleted_count} 个旧职位")
            return deleted_count
            
        except Exception as e:
            logger.error(f"清除旧职位失败: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()

    def clear_all_jobs(self) -> int:
        """
        清除所有职位记录
        
        Returns:
            删除的职位数量
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            cursor.execute('DELETE FROM jobs')
            deleted_count = cursor.rowcount
            
            # 重置自增ID
            cursor.execute('DELETE FROM sqlite_sequence WHERE name="jobs"')
            
            conn.commit()
            logger.info(f"已清除所有 {deleted_count} 个职位记录")
            return deleted_count
            
        except Exception as e:
            logger.error(f"清除所有职位失败: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()
    
    def search_jobs(
        self,
        keyword: str,
        site_label_keyword: str = '',
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> List[Dict]:
        """
        搜索职位
        
        Args:
            keyword: 搜索关键词
        
        Returns:
            匹配的职位列表
        """
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            
            conditions = []
            params = []
            if keyword:
                conditions.append('(title LIKE ? OR company LIKE ? OR description LIKE ?)')
                pattern = f'%{keyword}%'
                params.extend([pattern, pattern, pattern])
            if site_label_keyword:
                conditions.append(
                    "COALESCE(NULLIF(site_label, ''), source_site, '') LIKE ?"
                )
                params.append(f'%{site_label_keyword}%')

            query = 'SELECT * FROM jobs'
            if conditions:
                query += ' WHERE ' + ' AND '.join(conditions)
            query += '''
                ORDER BY
                    COALESCE(
                        datetime(NULLIF(TRIM(publish_time), '未知')),
                        datetime(found_time)
                    ) DESC,
                    datetime(found_time) DESC,
                    id DESC
            '''
            if limit is not None:
                query += ' LIMIT ? OFFSET ?'
                params.extend([limit, offset])
            cursor.execute(query, params)
            
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
            
        except Exception as e:
            logger.error(f"搜索职位失败: {e}")
            return []
        finally:
            conn.close()

    def count_jobs(self, keyword: str = '', site_label_keyword: str = '') -> int:
        """统计全部职位或搜索结果数量。"""
        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            conditions = []
            params = []
            if keyword:
                pattern = f'%{keyword}%'
                conditions.append(
                    '(title LIKE ? OR company LIKE ? OR description LIKE ?)'
                )
                params.extend([pattern, pattern, pattern])
            if site_label_keyword:
                conditions.append(
                    "COALESCE(NULLIF(site_label, ''), source_site, '') LIKE ?"
                )
                params.append(f'%{site_label_keyword}%')

            query = 'SELECT COUNT(*) AS total FROM jobs'
            if conditions:
                query += ' WHERE ' + ' AND '.join(conditions)
            cursor.execute(query, params)
            return int(cursor.fetchone()['total'])
        finally:
            conn.close()

    def backfill_site_labels(self, source_label_map: Dict[str, str]) -> int:
        """按来源域名为旧职位补齐网址标签，仅处理当前为空的记录。"""
        if not source_label_map:
            return 0

        conn = self._get_connection()
        try:
            cursor = conn.cursor()
            updated = 0
            for source_site, site_label in source_label_map.items():
                clean_source = str(source_site or '').strip().lower()
                clean_label = str(site_label or '').strip()
                if not clean_source or not clean_label:
                    continue
                cursor.execute(
                    '''
                    UPDATE jobs
                    SET site_label = ?
                    WHERE TRIM(COALESCE(site_label, '')) = ''
                      AND LOWER(TRIM(COALESCE(source_site, ''))) = ?
                    ''',
                    (clean_label, clean_source),
                )
                updated += cursor.rowcount
            conn.commit()
            return updated
        except Exception as e:
            logger.error(f"补齐旧职位网址标签失败: {e}")
            conn.rollback()
            return 0
        finally:
            conn.close()


# 全局数据库实例
db = JobDatabase()
