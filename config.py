from dataclasses import dataclass, field


# ============================================================
# API / 网络请求相关
# ============================================================

@dataclass
class NetworkConfig:
    """网络请求与 API 调用参数"""

    # ---- 请求超时（秒） ----
    api_timeout: int = 20
    """API 请求超时时间（秒）。过大会导致卡顿，过小可能在网络波动时失败"""

    # ---- 全局 API 并发 ----
    global_api_semaphore: int = 8
    """全局 API 并发请求信号量大小，控制同时发起的 API 请求数量"""
    api_semaphore_timeout: float = 30.0
    """API 信号量获取超时（秒），超时降级为不限并发"""

    # ---- 用户代理 ----
    user_agent: str = "Mozilla/5.0"
    """HTTP 请求 User-Agent 头"""

    # ---- 玩家同步轮询参数 ----
    sync_poll_intervals: list[float] = field(default_factory=lambda: [0.0, 3.0, 1.0, 1.5])
    """同步后的轮询间隔序列（秒）。[0.0, 3.0, 1.0, 1.5] 表示首轮立即检查（命中CD场景直接返回），
    若需要等待新数据则先等待3秒（给DAK.GG充足同步时间），后续按1s/1.5s渐进检查。
    时序：首轮(~0s) → 次轮(~3.2s) → 三轮(~4.4s) → 四轮(~5.9s)兜底。"""

    # ---- 分页与查询 ----
    max_match_pages: int = 20
    """全量拉取对局时的最大页数"""

    # ---- 语言 / 默认值 ----
    default_hl: str = "zh_CN"
    """API 语言参数"""
    default_team_mode: str = "SQUAD"
    """排行榜默认队伍模式"""


# ============================================================
# 缓存 & 锁相关
# ============================================================

@dataclass
class CacheConfig:
    """缓存容量 & 生命周期"""

    # ---- TTL 缓存 ----
    ttl_max_entries: int = 1024
    """TTL 缓存最大条目数，超过后自动清理过期或清空"""

    # ---- 结果图片缓存 ----
    result_image_ttl: int = 60
    """查询结果图片在内存中缓存的时长（秒）"""

    # ---- 角色评级排行结果图片缓存 ----
    character_ranking_ttl: int = 1800
    """角色评级排行结果图片在内存中缓存的时长（秒），半小时内同一段位排行直接命中缓存"""

    # ---- 同步跳过 TTL ----
    sync_skip_ttl: int = 300
    """玩家数据同步的最小间隔（秒），5 分钟内不重复同步"""

    # ---- 排行榜缓存 ----
    leaderboard_cache_ttl: int = 120
    """排行榜数据缓存时长（秒），实际运行中使用 2 分钟以保证数据新鲜度"""

    # ---- PENDING 查询去重 ----
    pending_max_age: float = 120.0
    """pending 查询条目最大存活时间（秒），超时后自动清理"""
    pending_cleanup_interval: float = 30.0
    """pending 过期条目的后台清理间隔（秒）"""
    pending_wait_timeout: int = 60
    """等待 pending 查询结果的最大时间（秒）"""


@dataclass
class LockConfig:
    """锁获取超时参数"""

    ttl_cache_lock_timeout: float = 5.0
    """TTL 缓存锁获取超时（秒），超时降级为未命中"""

    pending_lock_timeout: float = 10.0
    """PENDING 查询锁获取超时（秒），超时降级为独立查询"""

    time_lock_timeout: float = 10.0
    """时间同步锁获取超时（秒），超时使用本地时间兜底"""

    icon_cache_lock_timeout: float = 10.0
    """图标缓存锁获取超时（秒），超时跳过缓存"""

    paw_cache_lock_timeout: float = 10.0
    """猫爪水印缓存锁获取超时（秒），超时跳过缓存"""

    paw_base_lock_timeout: float = 10.0
    """猫爪原图锁获取超时（秒），超时直接从磁盘加载"""

    render_semaphore_timeout: float = 300.0
    """渲染并发信号量获取超时（秒），超时降级为不限并发"""


# ============================================================
# 渲染相关
# ============================================================

@dataclass
class RenderConfig:
    """图片渲染参数（修改需谨慎，部分值影响布局）"""

    # ---- 并发控制 ----
    max_render_concurrency: int = 2
    """同时进行的最大渲染任务数，避免 CPU 占满"""

    # ---- 画布尺寸 ----
    canvas_width: int = 1080
    """画布宽度（像素）"""
    header_height: int = 210
    """头部区域高度（像素）"""
    summary_height: int = 190
    """汇总统计区高度（像素）"""
    heroes_height: int = 210
    """常用英雄区高度（像素）"""
    match_height: int = 140
    """单条对局记录高度（像素）"""
    footer_height: int = 50
    """底部页脚高度（像素）"""
    padding: int = 20
    """模块间内边距（像素）"""

    # ---- 图标缓存 ----
    icon_cache_max_size: int = 4096
    """图标缓存最大条目数，超过后清空一半"""

    # ---- 猫爪水印缓存 ----
    paw_cache_max_size: int = 128
    """猫爪水印缓存最大条目数"""

    # ---- 字体大小 ----
    font_title_size: int = 40
    """玩家名字体大小"""
    font_big_size: int = 30
    """大数字 / 名次 / KDA / MMR 字体大小"""
    font_module_size: int = 24
    """模块标题字体大小"""
    font_body_size: int = 20
    """正文字体大小"""
    font_small_size: int = 18
    """小字说明字体大小"""
    font_mini_size: int = 12
    """极小字体大小（折线图坐标 / 战术等级）"""

    # ---- 颜色方案 ----
    bg: tuple[int, int, int] = (255, 255, 255)
    """整体背景色（RGB）"""
    card: tuple[int, int, int] = (255, 240, 245)
    """主卡片背景色（RGB）"""
    sub_card: tuple[int, int, int] = (255, 228, 238)
    """子卡片背景色（RGB）"""
    text: tuple[int, int, int] = (60, 30, 45)
    """主文字颜色（RGB）"""
    accent: tuple[int, int, int] = (255, 100, 130)
    """强调色（RGB）"""
    pink_border: tuple[int, int, int] = (255, 200, 210)
    """粉色边框色（RGB）"""


# ============================================================
# 查询相关
# ============================================================

@dataclass
class QueryConfig:
    """查询参数默认值"""

    default_match_count: int = 10
    """默认查询场次"""
    max_match_count: int = 20
    """最大查询场次"""
    match_count: int = 10
    """渲染数据构建时的默认查询场次数"""

    # ---- 角色统计 ----
    character_stats_dt: int = 7
    """角色统计查询的天数范围（最近7日）"""
    default_matching_mode: str = "RANK"
    """默认匹配模式（RANK / CASUAL / ALL）"""
    default_team_mode: str = "SQUAD"
    """默认队伍模式（SOLO / DUO / SQUAD）"""
    default_tier: str = "diamond_plus"
    """默认段位筛选（diamond_plus / all 等）"""

    # ---- 时间同步 ----
    time_sync_interval: int = 43200  # 12 小时
    """网络时间同步间隔（秒），默认 12 小时"""

    # ---- 清理 ----
    output_cleanup_days: int = 1
    """输出目录自动清理天数"""

    # ---- 排行榜 ----
    default_server: str = "seoul"
    """排行榜默认服务器"""

    # ---- 赛季兜底 ----
    fallback_season: str = "SEASON_20"
    """API 不可用时的兜底赛季，通过 /更新资源 可自动刷新"""


# ============================================================
# 映射相关
# ============================================================

@dataclass
class MapperConfig:
    """数据映射常量"""

    # ---- 常用英雄 ----
    common_heroes_limit: int = 5
    """常用英雄显示数量"""

    # ---- MMR 折线图 ----
    mmr_chart_points: int = 6
    """MMR 折线图显示的数据点数量"""


# ============================================================
# 资源下载相关（get_image.py）
# ============================================================

@dataclass
class ResourceConfig:
    """资源下载参数"""

    download_timeout: int = 20
    """资源下载超时（秒）"""
    download_sleep: float = 1.0
    """每次下载完成后的间隔（秒）"""
    download_concurrency: int = 6
    """变更/缺失资源的并发下载线程数"""
    chunk_size: int = 8192
    """下载时每块读取的字节数"""


NETWORK = NetworkConfig()
CACHE = CacheConfig()
LOCK = LockConfig()
RENDER = RenderConfig()
QUERY = QueryConfig()
MAPPER = MapperConfig()
RESOURCE = ResourceConfig()