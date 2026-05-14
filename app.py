#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
北京市养老服务全业务线招标监控系统 — Flask 后端
=============================================
覆盖业务：社区养老 | 居家照护 | 机构养老 | 驿站 | 健康体检 | 医疗诊所
         医养结合 | 康复 | 适老化改造 | 物业管理 | 园林绿化 | 餐饮
         养老护理员培训 | 长照师培训 | 长护险 | 老干部健康 | 助浴
功能：
  /api/fetch        — 手工触发抓取
  /api/bids         — 获取招标数据 JSON
  /api/email        — 发送邮件报告
  /                 — 前端页面
  /api/status       — 运行状态
定时任务：每日 09:00 自动抓取+发送邮件
"""

import os, re, json, time, logging, smtplib, threading, ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header

import requests
from bs4 import BeautifulSoup
import schedule
from flask import Flask, request, jsonify, send_from_directory, make_response
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 抓取进度跟踪
import threading
scrape_progress = {
    'running': False,
    'current_page': '',
    'source_name': '',
    'items_found': 0,
    'total_found': 0,
    'status': 'idle',
    'message': '',
}
scrape_lock = threading.Lock()

def update_scrape_progress(**kw):
    with scrape_lock:
        scrape_progress.update(kw)

app = Flask(__name__, static_folder='.', static_url_path='')

# CORS 支持：允许 file:// 和跨域访问
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

# ============ 配置 ============
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')

DEFAULT_CONFIG = {
    'email': {
        'smtp_host': 'smtp.qq.com',
        'smtp_port': 465,
        'smtp_user': '1298597748@qq.com',
        'smtp_pass': 'logilkqpkklxheab',
        'recipient': ''
    },
    'keywords': {
        # ══════════════════════════════════════════════════════════════
        # 关键词体系 — 按公司业务线分层（招投标组负责人视角）
        # 原则：宽进严出 → 关键词宽松匹配，详情页截止日期核实才是主把关
        # 顺序：最具体匹配在前 → 宽泛兜底在后（classify_bid 按迭代顺序首次命中即返回）
        # ══════════════════════════════════════════════════════════════

        # ── 第〇层：废标/终止/流标公告（最优先识别，避免被宽泛关键词吞掉）──
        '废标公告':       r'废标|终止公告|流标公告|项目终止|采购终止|废标公告|废标结果',

        # ── 第一层：超具体组合（避免被后续宽泛模式误吞噬）──
        '老干部健康':     r'(?:老干|军休|退休|离休).*?(?:体检|健康检查|健康体检|医疗|保健|疗养)|(?:体检|健康检查).*?(?:老干|军休|退休|离休|干部)',
        '干部休养所':     r'干部休养所|干休所|军休所|军队离休|军队退休|荣军|退役军人.*?休养',
        '残疾人服务':     r'残疾.*?(?:康复|助浴|洗浴|沐浴|服务|照护|托养|辅助)|残障.*?(?:康复|服务|照护)',

        # ── 第二层：特定业务/政策（高价值、窄匹配）──
        '长照师培训':     r'长照师|长期照护师|长期照护.*?培训|失能.*?照护.*?培训',
        '长期护理险':     r'长期护理|长护险|护理保险|长期照护保险',
        '巡视探访':       r'巡视探访|探访服务|关爱.*?走访|入户.*?探访|独居.*?探访',
        '医养结合':       r'医养结合|医养融合|医养.*?服务|医养.*?项目',

        # ── 第三层：培训/评估（比养老/康复更具体）──
        '养老护理员培训': r'养老护理|护理员.*?培训|护理.*?技能|护工.*?培训|养老.*?护理员',
        '养老技能培训':   r'养老.*?培训|老年.*?照护.*?培训|养老服务.*?培训|长护.*?培训',
        '适老化评估':     r'适老化评估|适老.*?评估|老年.*?能力评估|老年人.*?评估|老年人.*?能力',
        '康复评估':       r'康复评估|康复.*?评定',

        # ── 第四层：适老化改造（在「养老」之前，避免被吞噬）──
        '适老化改造':     r'适老化|适老改造|无障碍改造|居家.*?适老|老年.*?宜居|居家.*?无障碍',

        # ── 第五层：居家/社区/机构 核心服务 ──
        '养老服务驿站':   r'养老.*?驿站|养老服务.*?站|社区.*?养老.*?驿站|日间照料.*?驿站|养老服务.*?中心',
        '居家照护':       r'居家照护|居家护理|上门照护|上门.*?护理|家庭.*?照护|失能.*?照护',
        '居家养老':       r'居家养老|居家.*?养老服务|居家.*?为老|上门.*?养老',
        '家庭医疗':       r'家庭医疗|居家医疗|上门医疗|家庭医生|护士上门|入户.*?医疗|上门.*?诊疗',
        '机构养老':       r'机构养老|养老机构.*?运营|养老机构.*?管理|养老院|敬老院|福利院.*?养老|老年.*?公寓.*?运营',
        '社区养老':       r'社区养老|社区.*?为老|社区.*?日照|社区.*?日间照料|社区.*?助老|社区.*?居[家老]',

        # ── 第六层：配套服务 ──
        '园林绿化':       r'园林绿化|景观绿化|绿化工程|绿化养护|公园绿化|绿化.*?管护|绿化.*?项目|园艺|花木|苗木|绿地.*?养护',
        '餐饮服务':       r'餐饮服务|配餐|送餐|老年.*?餐|助餐|食堂.*?运营|食堂.*?服务|老年.*?食堂|长者.*?食堂',
        '物业管理':       r'物业(?!.*?(?:费|维修基金|专项维修)).*?(?:管理|服务|项目|采购|招标|保洁|保安|绿化)|保洁.*?服务|保安.*?服务|后勤.*?服务',
        '助浴服务':       r'助浴|洗浴.*?服务|沐浴.*?服务|上门.*?助浴|上门.*?洗浴',

        # ── 第七层：健康体检/康复（宽泛匹配）──
        '健康体检':       r'健康体检|体检服务|体检采购|体检项目|体检.*?招标|健康检查|健康筛查|健康管理',
        '体检':           r'体检(?!.*?(?:阶段|环节|名单|入围|成绩|资格复审|考察))',  # 排除人事招聘
        '康复服务':       r'康复服务|康复治疗|康复训练|康复.*?项目|运动康复|体适能|肢体训练|功能训练',

        # ── 第八层：最宽泛兜底（必须在最后，避免误吞噬）──
        '康复':           r'康复(?!.*?(?:护理|医疗|医院|中心|学科|医学|门诊))',  # 全量回收，排除医疗机构名
        '养老':           r'养老(?!金|保险金|保险|保障|社保)',  # 全量兜底，排除纯养老金/社保
    },
    'schedule_time': '09:00',
    'cors_proxy': '',  # 如需代理可填
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
            # merge defaults
            for k, v in DEFAULT_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

# ============ 数据存储 ============
DATA_FILE = os.path.join(os.path.dirname(__file__), 'bids_data.json')

def load_bids():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_bids(bids):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(bids, f, ensure_ascii=False, indent=2)

# ============ 辅助构建函数（减少重复代码）============
def build_url(href, base):
    """统一补全URL"""
    if href.startswith('//'): return 'http:' + href
    if href.startswith('/'): return base.rstrip('/') + href
    if href.startswith('http'): return href
    return None

def safe_href(a_tag):
    """安全获取a标签href"""
    h = a_tag.get('href', '')
    if isinstance(h, list): h = h[0] if h else ''
    return h if isinstance(h, str) else ''

# ============ 工具函数 ============
# ══════════════════════════════════════════════════════════════
# 非招标排除规则（招投标组负责人视角：只要采购/招标公告，不要政策宣传/办事指南）
# ══════════════════════════════════════════════════════════════
NON_BIDDING_PATTERNS = (
    # ── 办事指南/服务申办 ──
    r'怎么领|怎么用|怎么申请|如何申请|如何办理|如何申领|如何领取|怎样申请|怎样才能'
    r'|网上申办|网上办理|在线申办|办事指南|申领指南|办理指南|服务指南|操作指南'
    r'|申办流程|申请流程|办理流程|办事流程'
    # ── 政策宣传/图解 ──
    r'|一图看|一图读懂|图解|图看懂|带您了解|带您读懂'
    r'|政策问答|政策宣传|政策.*解读|政策.*图解|政策.*宣讲|政策.*问答'
    r'|政策文件|规范性文件|法规草案|规章.*解读'
    # ── 卡/补贴申领类 ──
    r'|养老助残卡.*申|养老卡.*申领|助残卡.*申领|老年卡.*申领'
    r'|补贴.*怎么领|补贴.*如何申|补贴.*申领|补贴.*领取|补贴.*申请|补贴.*发放'
    # ── 促销/自媒体风格 ──
    r'|速看|必看.*收藏|转发.*收藏|请您查收|提醒您|注意啦|告诉您|好消息|福利来了'
    # ── 非招标公告类（已中标/已签约/已验收 = 没机会投了）──
    r'|调查问卷|问卷调查|满意度|征求意见|公示.*名单|公开招聘|面试|成绩'
    # ── 中标公告/成交公告 ──
    r'|中标.*公示|中标.*公告|中标.*结果|成交.*公示|成交.*公告|成交.*结果|结果.*公告|结果.*公示'
    r'|合同.*公示|合同.*公告|政府采购合同|采购合同.*公告|验收.*公告|验收.*公示|履约.*公示'
    # ── 老年人能力综合评估（非招标项目）──
    r'|老年人能力综合评估'
    # ── 设备/器械采购 ──
    r'|设备.*采购|设备.*购置|采购.*设备|购置.*设备'
    r'|器械.*采购|器械.*购置|采购.*器械|购置.*器械'
    r'|医疗设备|康复设备|康复器材|康复器械|辅助器具|适老设备'
    # ── 养老/福利领域资格公示（非招标，是入住资格/补贴资格等行政审核公示）──
    r'|入住资格|优待服务保障对象|拟入住|接收.*公示$'
    # ── 服务门户/查询入口 ──
    r'|我要查询|在线查询|查询.*结果|查询.*入口|在线服务|网上服务'
    # ── 新闻/报道类 ──
    r'|首家.*投用|首个.*投用|正式投用|正式运营|开业|揭牌|启动.*仪式'
    # ── 纯政策/福利介绍（无采购动作）──
    r'|津贴补贴(?!.*(?:采购|招标|购买|委托|遴选|比选))'
    r'|一件事(?!.*(?:采购|招标|购买|服务))'  # "一件事一次办"是政务服务整合，非招标
    # ── 太短或纯机构名（大概率是导航页/机构介绍）──
    r'|^.{1,8}$'  # 标题≤8字的几乎不可能是招标公告
    r'|^.{2,15}(?:局|委员会|中心|办公室|处|站|所|协会)$'  # 纯机构名收尾无采购动作
)

# 详情页截止日期提取缓存（URL -> (时间, 截止日期字符串或None)）
DETAIL_CACHE = {}
CACHE_DURATION = timedelta(hours=24)

def classify_bid(title, config):
    """V1.0 风格关键词匹配：宽进——意思相近就留，仅排除明确的非招标内容"""
    if re.search(NON_BIDDING_PATTERNS, title):
        return None
    for cat, pattern in config['keywords'].items():
        if re.search(pattern, title):
            return cat
    return None

def parse_date(s):
    """解析日期字符串，支持多种格式"""
    if not s:
        return None
    # 格式1：2026年5月14日、2026-05-14、2026/05/14 等
    m = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', str(s))
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]))
        except Exception:
            pass
    # 格式2：英文月份 May 14, 2026 / 14 May 2026 等
    month_map = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
        'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    }
    m2 = re.search(r'([a-zA-Z]+)\s+(\d{1,2}),?\s+(\d{4})', str(s))
    if m2:
        month_name = m2.group(1).lower()[:3]
        if month_name in month_map:
            try:
                return datetime(int(m2.group(3)), month_map[month_name], int(m2.group(2)))
            except Exception:
                pass
    m3 = re.search(r'(\d{1,2})\s+([a-zA-Z]+)\s+(\d{4})', str(s))
    if m3:
        month_name = m3.group(2).lower()[:3]
        if month_name in month_map:
            try:
                return datetime(int(m3.group(3)), month_map[month_name], int(m3.group(1)))
            except Exception:
                pass
    return None

def calc_urgency(deadline_str):
    dl = parse_date(deadline_str)
    if not dl: return {'level': 'normal', 'days': None, 'label': '日期待核实'}
    now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    dl = dl.replace(hour=0, minute=0, second=0, microsecond=0)
    days = (dl - now).days
    if days < 0: return {'level': 'expired', 'days': days, 'label': '已过期'}
    if days <= 3: return {'level': 'critical', 'days': days, 'label': f'仅剩{days}天'}
    if days <= 7: return {'level': 'warning', 'days': days, 'label': f'剩余{days}天'}
    return {'level': 'normal', 'days': days, 'label': f'{days}天'}

# ============ 网页抓取 ============
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}

# SSL 适配器：处理 BAD_ECPOINT 等错误
# ggzyfw.beijing.gov.cn 的 ECC 实现有 bug，需要禁用所有 ECDHE/ECDSA 密码套件
FALLBACK_CIPHERS = 'AES256-GCM-SHA384:AES128-GCM-SHA256:AES256-SHA:AES128-SHA:@SECLEVEL=0'

class NoSSLAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # 只使用 RSA 密码套件，避免 ECC BAD_ECPOINT 错误
        ctx.set_ciphers(FALLBACK_CIPHERS)
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)

def fetch_page(url, timeout=15, verify=True):
    """抓取单个页面"""
    session = requests.Session()
    if not verify:
        session.mount('https://', NoSSLAdapter())
    try:
        resp = session.get(url, headers=HEADERS, timeout=timeout)
        resp.encoding = resp.apparent_encoding or 'utf-8'
        return resp.text
    except requests.exceptions.SSLError:
        if verify:
            raise
        # 回退到 urllib，同样使用非 ECC 密码套件
        import urllib.request, urllib.error
        uctx = ssl.create_default_context()
        uctx.check_hostname = False
        uctx.verify_mode = ssl.CERT_NONE
        uctx.set_ciphers(FALLBACK_CIPHERS)
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout, context=uctx) as resp:
            data = resp.read()
        # 尝试检测编码
        ct = resp.headers.get('Content-Type', '').lower()
        charset = 'utf-8'
        if 'charset=' in ct:
            charset = ct.split('charset=')[-1].split(';')[0].strip()
        return data.decode(charset, errors='replace')

def make_soup(html):
    """创建 BeautifulSoup，优先 lxml，失败回退 html.parser"""
    try:
        return BeautifulSoup(html, 'lxml')
    except Exception:
        return BeautifulSoup(html, 'html.parser')

def extract_deadline_from_detail(url):
    now = datetime.now()
    if url in DETAIL_CACHE:
        ct, cr = DETAIL_CACHE[url]
        if now - ct < CACHE_DURATION:
            return cr
    try:
        html = fetch_page(url, timeout=12, verify=False)
        soup = make_soup(html)
        page_text = soup.get_text()
        # 组合截止日期匹配模式
        deadline_re = r'(?:资料提交|报送材料|响应文件递交|提交投标文件|投标|报名)截止[日期时间]{0,2}\s*[:：]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})'
        m = re.search(deadline_re, page_text)
        if not m:
            m = re.search(r'截止[日期时间]{0,2}\s*[:：]?\s*(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})', page_text)
        if m:
            pd = parse_date(m.group(1))
            if pd:
                r = pd.strftime('%Y-%m-%d')
                DETAIL_CACHE[url] = (now, r)
                logging.info(f'[详情页截止日期] {url} -> {r}')
                return r
        # 元素级匹配
        for e in soup.find_all(['span', 'td', 'p', 'div']):
            t = e.get_text(strip=True)
            if re.search(r'截止|期限|deadline|递交|提交', t, re.I):
                dm = re.search(r'(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})', t)
                if dm:
                    pd = parse_date(dm.group(1))
                    if pd:
                        r = pd.strftime('%Y-%m-%d')
                        DETAIL_CACHE[url] = (now, r)
                        logging.info(f'[详情页截止日期(元素)] {url} -> {r}')
                        return r
        DETAIL_CACHE[url] = (now, None)
        return None
    except Exception as e:
        logging.warning(f'提取详情页截止日期失败 {url}: {e}')
        DETAIL_CACHE[url] = (now, None)
        return None


# 废标原因提取缓存
CANCEL_REASON_CACHE = {}

def extract_cancel_reason_from_detail(url):
    """从详情页提取废标/终止原因"""
    now = datetime.now()
    if url in CANCEL_REASON_CACHE:
        ct, cr = CANCEL_REASON_CACHE[url]
        if now - ct < CACHE_DURATION:
            return cr
    try:
        html = fetch_page(url, timeout=12, verify=False)
        soup = make_soup(html)
        page_text = soup.get_text()
        # 常见废标原因表述模式
        patterns = [
            r'(?:废标原因|终止原因|废标理由|流标原因|项目废标的原因)[：:\s]*([^。；\n]{4,80})',
            r'因([^，。；\n]{2,60})(?:，|,)\s*(?:本)?项目[^，。；]{0,10}(?:废标|终止|流标)',
            r'([^。；\n]{2,50}(?:不足|不够|不符合|未达到)[^。；\n]{2,50})(?:[。；，,\n]|$)',
        ]
        for pat in patterns:
            m = re.search(pat, page_text)
            if m:
                reason = m.group(1).strip()
                # 清理长空白和多余标点
                reason = re.sub(r'\s+', ' ', reason)
                reason = re.sub(r'^[：:\s]+|[：:\s]+$', '', reason)
                if len(reason) >= 4:
                    CANCEL_REASON_CACHE[url] = (now, reason)
                    logging.info(f'[废标原因] {url} -> {reason[:60]}')
                    return reason
        CANCEL_REASON_CACHE[url] = (now, None)
        return None
    except Exception as e:
        logging.warning(f'提取废标原因失败 {url}: {e}')
        CANCEL_REASON_CACHE[url] = (now, None)
        return None






# ccgp 网站非招标路径黑名单
CCGP_SKIP_PATHS = [
    r'/zcfg/',      # 政策法规
    r'/jdgl/',      # 监督管理
    r'/jdjc/',      # 监督检查
    r'/zbdc/',      # 招标调查
    r'/zdtj/',      # 制度统计
    r'/bszn/',      # 办事指南
    r'/zhxx/',      # 综合信息
    r'/gywm/',      # 关于我们
]

def scrape_ccgp(config):
    """抓取北京市政府采购网——iframe列表页（市级+区级，各30页，8线程并行）"""
    results = []
    seen = set()
    base = 'http://www.ccgp-beijing.gov.cn'

    # 仅保留实际有效的 iframe 数据源（/cggg/ 和 /zbgg/ 全部404已移除，首页仅有导航链接也无用）
    # 市级信息公告 + 区级信息公告（含废标/终止公告，每页14条，静态HTML可直接抓取）
    page_urls = []
    for prefix in ['/xxgg/sjxxgg/A002004001index_', '/xxgg/qjxxgg/A002004002index_']:
        for i in range(1, 31):  # 各30页，覆盖近期全部数据
            page_urls.append(base + prefix + str(i) + '.htm')

    def _process_page(page_url):
        """处理单个列表页（线程安全，不修改共享状态）"""
        items = []
        page_seen = set()
        try:
            html = fetch_page(page_url, timeout=12)
            soup = make_soup(html)
            for a_tag in soup.find_all('a', href=True):
                href = a_tag['href'] if isinstance(a_tag.get('href'), str) else ''
                title = a_tag.get_text(strip=True)
                if not title or len(title) < 6:
                    continue

                if href.startswith('//'): full_url = 'http:' + href
                elif href.startswith('/'): full_url = base + href
                elif href.startswith('http'): full_url = href
                else: continue

                if any(re.search(pat, full_url) for pat in CCGP_SKIP_PATHS):
                    continue
                if re.search(r'city=|name=', full_url):
                    continue
                if 'mkt-' in full_url or 'mall-view' in full_url:
                    continue
                if re.search(r'/index(?:_\d+)?\.html?$', full_url):
                    continue
                if full_url in page_seen:
                    continue
                page_seen.add(full_url)

                category = classify_bid(title, config)
                if not category:
                    continue

                # 优先从列表页 datetime span 取发布时间（比URL日期更准确）
                pub_date = ''
                dt_span = a_tag.find_next('span', class_='datetime')
                if not dt_span:
                    dt_span = a_tag.parent.find('span', class_='datetime') if a_tag.parent else None
                if dt_span:
                    dt_text = dt_span.get_text(strip=True)
                    dm = re.search(r'(\d{4}-\d{1,2}-\d{1,2})', dt_text)
                    if dm:
                        pub_date = dm.group(1)
                # 兜底：从URL路径提取日期
                if not pub_date:
                    um = re.search(r'/(\d{4})[-/](\d{1,2})[-/](\d{1,2})[/.]', full_url)
                    if um:
                        pub_date = f'{um[1]}-{um[2].zfill(2)}-{um[3].zfill(2)}'
                if not pub_date:
                    um = re.search(r'/(\d{4})/(\d{1,2})/', full_url)
                    if um:
                        ud = datetime(int(um[1]), int(um[2]), 1)
                        if ud <= datetime.now():
                            pub_date = f'{um[1]}-{um[2].zfill(2)}-01'

                deadline = ''
                if pub_date:
                    pd_dt = parse_date(pub_date)
                    if pd_dt:
                        dl = pd_dt + timedelta(days=20)
                        deadline = dl.strftime('%Y-%m-%d')

                items.append({
                    'id': str(hash(full_url)),
                    'title': title,
                    'url': full_url,
                    'pubDate': pub_date or '未知',
                    'deadline': deadline or '待确认',
                    'cancelReason': '',
                    'source': '北京市政府采购网',
                    'category': category,
                    'urgency': calc_urgency(deadline),
                    'fetchedAt': datetime.now().isoformat()
                })
        except Exception as e:
            logging.warning(f'抓取 CCGP 子页 {page_url} 失败: {e}')
        return items

    # 8线程并行抓取60页，速度提升约8倍
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_process_page, url): url for url in page_urls}
        for fut in as_completed(futures):
            for item in fut.result():
                if item['url'] not in seen:
                    seen.add(item['url'])
                    results.append(item)

    logging.info(f'[CCGP] {len(page_urls)} 页并行抓取 → {len(results)} 条匹配')
    return results

def scrape_ggzy(config):
    """抓取北京市公共资源交易服务平台"""
    results = []
    seen = set()
    try:
        urls = [
            ('https://ggzyfw.beijing.gov.cn/xmcgxqgg/index.html', '采购需求公示'),
            ('https://ggzyfw.beijing.gov.cn/jyxxggjtbyqs/index.html', '交易公告'),
            ('https://ggzyfw.beijing.gov.cn/jyxxggjtbyqs/index_1.html', '交易公告2'),
            ('https://ggzyfw.beijing.gov.cn/jyxxggjtbyqs/index_2.html', '交易公告3'),
            ('https://ggzyfw.beijing.gov.cn/jyxxggjtbyqs/index_3.html', '交易公告4'),
        ]
        for page_url, page_label in urls:
            try:
                html = fetch_page(page_url, timeout=12, verify=False)
                soup = make_soup(html)
                for a_tag in soup.find_all('a', href=True):
                    href = a_tag['href'] if isinstance(a_tag.get('href'), str) else ''
                    title = a_tag.get_text(strip=True)
                    if not title or len(title) < 8:
                        continue

                    if href.startswith('/'):
                        full_url = 'https://ggzyfw.beijing.gov.cn' + href
                    elif href.startswith('http'):
                        full_url = href
                    else:
                        continue

                    # 过滤：排除导航链接（index.html、首页、列表页）
                    if full_url.endswith('index.html') or full_url.endswith('/'):
                        continue
                    # 必须包含数字ID的路径（真实公告才有）
                    if not re.search(r'/(\d{4,})/', full_url):
                        continue
                    if full_url in seen:
                        continue
                    seen.add(full_url)

                    # 分类（宽进：关键词匹配即可，不要求标题含招标/采购字样）
                    category = classify_bid(title, config)
                    if not category:
                        continue

                    # 从 URL 提取日期
                    pub_date = ''
                    um = re.search(r'/(\d{4})(\d{2})(\d{2})/', full_url)
                    if um:
                        pub_date = f'{um[1]}-{um[2]}-{um[3]}'

                    # 截止日期：对于采购需求公示，通常发布后约7-20天
                    deadline = ''
                    if pub_date:
                        pd = parse_date(pub_date)
                        if pd:
                            dl = pd + timedelta(days=20)
                            deadline = dl.strftime('%Y-%m-%d')

                    results.append({
                        'id': str(hash(full_url)),
                        'title': title,
                        'url': full_url,
                        'pubDate': pub_date or '未知',
                        'deadline': deadline or '待确认',
                        'cancelReason': '',
                        'source': '北京市公共资源交易服务平台',
                        'category': category,
                        'urgency': calc_urgency(deadline),
                        'fetchedAt': datetime.now().isoformat()
                    })
            except Exception as e:
                logging.warning(f'抓取 {page_url} 失败: {e}')
    except Exception as e:
        logging.error(f'抓取 ggzy 失败: {e}')
    return results

def scrape_laoganbu(config):
    """抓取北京市老干部局及各区老干部局官网"""
    results = []
    seen = set()
    # 老干部局相关网站列表
    targets = [
        ('https://www.bjlgb.gov.cn/', '北京市老干部局'),  # 市委老干部局
        ('http://www.bjhd.gov.cn/', '海淀区老干部局'),
        ('http://www.bjxch.gov.cn/', '西城区老干部局'),
        ('http://www.bjdch.gov.cn/', '东城区老干部局'),
        ('http://www.bjchy.gov.cn/', '朝阳区老干部局'),
        ('http://www.bjft.gov.cn/', '丰台区老干部局'),
        ('http://www.bjsjs.gov.cn/', '石景山区老干部局'),
        ('http://www.bjchp.gov.cn/', '昌平区老干部局'),
        ('http://www.bjdx.gov.cn/', '大兴区老干部局'),
        ('http://www.bjtzh.gov.cn/', '通州区老干部局'),
        ('http://www.bjshy.gov.cn/', '顺义区老干部局'),
        ('http://www.bjmtg.gov.cn/', '门头沟区老干部局'),
        ('http://www.bjhr.gov.cn/', '怀柔区老干部局'),
        ('http://www.bjpg.gov.cn/', '平谷区老干部局'),
        ('http://www.bjmy.gov.cn/', '密云区老干部局'),
        ('http://www.bjyq.gov.cn/', '延庆区老干部局'),
        ('http://www.bjfsh.gov.cn/', '房山区老干部局'),
    ]
    for url, name in targets:
        try:
            # 尝试访问通知公告/采购信息栏目
            for subpath in ['', '/tzgg/', '/xxgk/tzgg/', '/tzgg/index.shtml', '/col/colXXX/index.html']:
                try_url = url.rstrip('/') + subpath if subpath else url
                if try_url in seen:
                    continue
                seen.add(try_url)
                try:
                    html = fetch_page(try_url, timeout=8, verify=True)
                    soup = make_soup(html)
                    for a_tag in soup.find_all('a', href=True):
                        href = a_tag['href'] if isinstance(a_tag.get('href'), str) else ''
                        title = a_tag.get_text(strip=True)
                        if not title or len(title) < 6:
                            continue
                        # 补全URL
                        if href.startswith('//'):
                            full_url = 'http:' + href
                        elif href.startswith('/'):
                            full_url = url.rstrip('/') + href
                        elif href.startswith('http'):
                            full_url = href
                        else:
                            continue
                        if full_url in seen:
                            continue
                        seen.add(full_url)
                        # 关键词匹配
                        category = classify_bid(title, config)
                        if not category:
                            continue
                        results.append({
                            'id': str(hash(full_url)),
                            'title': title,
                            'url': full_url,
                            'pubDate': '未知',
                            'deadline': '待确认',
                            'cancelReason': '',
                            'source': name,
                            'category': category,
                            'urgency': calc_urgency(''),
                            'fetchedAt': datetime.now().isoformat()
                        })
                except Exception:
                    continue
        except Exception:
            pass
    return results


def scrape_junxiu(config):
    """抓取军队离休退休干部休养所相关公告"""
    results = []
    seen = set()
    # 北京市军休所相关网站
    targets = [
        ('http://www.bjjx.gov.cn/', '北京市军队离休退休干部安置事务中心'),
        ('http://tyjrswj.beijing.gov.cn/', '北京市退役军人事务局'),
    ]
    for url, name in targets:
        try:
            for subpath in ['', '/tzgg/', '/xxgk/tzgg/', '/gongkai/tzgg/']:
                try_url = url.rstrip('/') + subpath if subpath else url
                if try_url in seen:
                    continue
                seen.add(try_url)
                try:
                    html = fetch_page(try_url, timeout=8, verify=True)
                    soup = make_soup(html)
                    for a_tag in soup.find_all('a', href=True):
                        href = a_tag['href'] if isinstance(a_tag.get('href'), str) else ''
                        title = a_tag.get_text(strip=True)
                        if not title or len(title) < 6:
                            continue
                        if href.startswith('//'):
                            full_url = 'http:' + href
                        elif href.startswith('/'):
                            full_url = url.rstrip('/') + href
                        elif href.startswith('http'):
                            full_url = href
                        else:
                            continue
                        if full_url in seen:
                            continue
                        seen.add(full_url)
                        category = classify_bid(title, config)
                        if not category:
                            continue
                        results.append({
                            'id': str(hash(full_url)),
                            'title': title,
                            'url': full_url,
                            'pubDate': '未知',
                            'deadline': '待确认',
                            'cancelReason': '',
                            'source': name,
                            'category': category,
                            'urgency': calc_urgency(''),
                            'fetchedAt': datetime.now().isoformat()
                        })
                except Exception:
                    continue
        except Exception:
            pass
    return results




def scrape_cebpubservice(config):
    """抓取中国招标投标公共服务平台(采招网接口)"""
    results = []
    seen = set()
    keywords = ['养老', '康复', '物业', '体检', '老干部', '军休', '适老化', '居家', '护理']
    for kw in keywords:
        try:
            # Try chinabidding API (more accessible)
            search_url = f'https://www.chinabidding.cn/search/searchzbw/searchzbw2?keywords={kw}&page=1&area_id=110000&time=365'
            html = fetch_page(search_url, timeout=10, verify=True)
            soup = make_soup(html)
            for a_tag in soup.find_all('a', href=True):
                href = a_tag.get('href', '')
                if isinstance(href, list):
                    href = href[0] if href else ''
                if not isinstance(href, str):
                    href = str(href) if href else ''
                href = href.strip()
                title = a_tag.get_text(strip=True)
                if not title or len(title) < 6:
                    continue
                if href.startswith('//'):
                    full_url = 'http:' + href
                elif href.startswith('/'):
                    full_url = 'https://www.chinabidding.cn' + href
                elif href.startswith('http'):
                    full_url = href
                else:
                    continue
                if full_url in seen:
                    continue
                seen.add(full_url)
                category = classify_bid(title, config)
                if not category:
                    continue
                pub_date = ''
                pub_date = ''
                dm = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', title)
                if dm:
                    pub_date = f'{dm[1]}-{dm[2].zfill(2)}-{dm[3].zfill(2)}'
                deadline = ''
                if pub_date:
                    pd_dt = parse_date(pub_date)
                    if pd_dt:
                        dl = pd_dt + timedelta(days=20)
                        deadline = dl.strftime('%Y-%m-%d')
                results.append({
                    'id': str(hash(full_url)),
                    'title': title,
                    'url': full_url,
                    'pubDate': pub_date or '未知',
                    'deadline': deadline or '待确认',
                    'cancelReason': '',
                    'source': '中国采招网',
                    'category': category,
                    'urgency': calc_urgency(deadline),
                    'fetchedAt': datetime.now().isoformat()
                })
        except Exception as e:
            logging.warning(f'chinabidding {kw} 失败: {e}')
    return results


def scrape_ccgp_search(config):
    """通过中国政府采购网搜索接口获取北京相关招标公告"""
    results = []
    seen = set()
    keywords = ['养老', '康复', '物业', '体检', '老干部', '军休', '适老化', '居家养老', '残疾人']
    for kw in keywords:
        try:
            search_url = f'http://search.ccgp.gov.cn/bxsearch?searchtype=1&page_index=1&bidSort=0&buyerName=&projectId=&pinMu=0&bidType=0&dbselect=bidx&kw={kw}+北京&start_time=2025:01:01&end_time=&timeType=6&displayZone=&zoneId=&pppStatus=&agentName='
            html = fetch_page(search_url, timeout=15, verify=True)
            soup = make_soup(html)
            for a_tag in soup.find_all('a', href=True):
                href = a_tag.get('href', '')
                if isinstance(href, list):
                    href = href[0] if href else ''
                if not isinstance(href, str):
                    href = str(href) if href else ''
                href = href.strip()
                title = a_tag.get_text(strip=True)
                if not title or len(title) < 8:
                    continue
                if not href.startswith('http'):
                    continue
                if href in seen:
                    continue
                seen.add(href)
                category = classify_bid(title, config)
                if not category:
                    continue
                # extract date from title or html
                pub_date = ''
                um = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', title)
                if um:
                    pub_date = f'{um[1]}-{um[2].zfill(2)}-{um[3].zfill(2)}'
                deadline = ''
                if pub_date:
                    pd_dt = parse_date(pub_date)
                    if pd_dt:
                        dl = pd_dt + timedelta(days=20)
                        deadline = dl.strftime('%Y-%m-%d')
                results.append({
                    'id': str(hash(href)),
                    'title': title,
                    'url': href,
                    'pubDate': pub_date or '未知',
                    'deadline': deadline or '待确认',
                    'cancelReason': '',
                    'source': '中国政府采购网(搜索)',
                    'category': category,
                    'urgency': calc_urgency(deadline),
                    'fetchedAt': datetime.now().isoformat()
                })
        except Exception as e:
            logging.warning(f'ccgp_search {kw} 失败: {e}')
    return results





def scrape_mzj(config):
    """抓取北京市民政局（养老服务相关公告）"""
    results = []
    seen = set()
    urls = [
        'http://mzj.beijing.gov.cn/',
        'http://mzj.beijing.gov.cn/col/col4730/index.html',
        'http://mzj.beijing.gov.cn/col/col4732/index.html',
    ]
    for page_url in urls:
        try:
            html = fetch_page(page_url, timeout=10, verify=True)
            soup = make_soup(html)
            for a_tag in soup.find_all('a', href=True):
                href = a_tag.get('href', '')
                if isinstance(href, list):
                    href = href[0] if href else ''
                if not isinstance(href, str):
                    href = str(href) if href else ''
                href = href.strip()
                title = a_tag.get_text(strip=True)
                if not title or len(title) < 6:
                    continue
                if href.startswith('//'):
                    full_url = 'http:' + href
                elif href.startswith('/'):
                    full_url = 'http://mzj.beijing.gov.cn' + href
                elif href.startswith('http'):
                    full_url = href
                else:
                    continue
                if full_url in seen:
                    continue
                seen.add(full_url)
                category = classify_bid(title, config)
                if not category:
                    continue
                pub_date = ''
                dm = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', title)
                if dm:
                    pub_date = f'{dm[1]}-{dm[2].zfill(2)}-{dm[3].zfill(2)}'
                else:
                    dm2 = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', html)
                    if dm2:
                        pub_date = f'{dm2[1]}-{dm2[2].zfill(2)}-{dm2[3].zfill(2)}'
                deadline = ''
                if pub_date:
                    pd_dt = parse_date(pub_date)
                    if pd_dt:
                        dl = pd_dt + timedelta(days=20)
                        deadline = dl.strftime('%Y-%m-%d')
                results.append({
                    'id': str(hash(full_url)),
                    'title': title,
                    'url': full_url,
                    'pubDate': pub_date or '未知',
                    'deadline': deadline or '待确认',
                    'cancelReason': '',
                    'source': '北京市民政局',
                    'category': category,
                    'urgency': calc_urgency(deadline),
                    'fetchedAt': datetime.now().isoformat()
                })
        except Exception as e:
            logging.warning(f'民政局 {page_url} 失败: {e}')
    return results


def scrape_wjw(config):
    """抓取北京市卫生健康委员会 — 招标公告、采购意向"""
    results = []
    seen = set()
    base = 'https://wjw.beijing.gov.cn'
    urls = [
        base + '/',
        base + '/zwgk_20040/qt/tzgg/',
        base + '/zwgk_20040/qt/cgxx/',
        base + '/zwgk_20040/zfxxgk/cgys/',
    ]
    for page_url in urls:
        try:
            html = fetch_page(page_url, timeout=12, verify=True)
            soup = make_soup(html)
            for a_tag in soup.find_all('a', href=True):
                href = safe_href(a_tag)
                title = a_tag.get_text(strip=True)
                if not title or len(title) < 8:
                    continue
                full_url = build_url(href, base)
                if not full_url or full_url in seen:
                    continue
                seen.add(full_url)
                category = classify_bid(title, config)
                if not category:
                    continue
                pub_date = ''
                dm = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', title)
                if dm:
                    pub_date = f'{dm[1]}-{dm[2].zfill(2)}-{dm[3].zfill(2)}'
                deadline = ''
                if pub_date:
                    pd_dt = parse_date(pub_date)
                    if pd_dt:
                        dl = pd_dt + timedelta(days=20)
                        deadline = dl.strftime('%Y-%m-%d')
                results.append({
                    'id': str(hash(full_url)),
                    'title': title,
                    'url': full_url,
                    'pubDate': pub_date or '未知',
                    'deadline': deadline or '待确认',
                    'cancelReason': '',
                    'source': '北京市卫健委',
                    'category': category,
                    'urgency': calc_urgency(deadline),
                    'fetchedAt': datetime.now().isoformat()
                })
        except Exception as e:
            logging.warning(f'卫健委 {page_url} 失败: {e}')
    return results


def scrape_ybj(config):
    """抓取北京市医疗保障局 — 长护险、医保相关招标公告"""
    results = []
    seen = set()
    base = 'https://ybj.beijing.gov.cn'
    urls = [
        base + '/',
        base + '/zwgk/tzgg/',
        base + '/zwgk/zfcg/',
        base + '/zwgk/zfcg/cgys/',
    ]
    for page_url in urls:
        try:
            html = fetch_page(page_url, timeout=12, verify=True)
            soup = make_soup(html)
            for a_tag in soup.find_all('a', href=True):
                href = safe_href(a_tag)
                title = a_tag.get_text(strip=True)
                if not title or len(title) < 8:
                    continue
                full_url = build_url(href, base)
                if not full_url or full_url in seen:
                    continue
                seen.add(full_url)
                category = classify_bid(title, config)
                if not category:
                    continue
                pub_date = ''
                dm = re.search(r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', title)
                if dm:
                    pub_date = f'{dm[1]}-{dm[2].zfill(2)}-{dm[3].zfill(2)}'
                deadline = ''
                if pub_date:
                    pd_dt = parse_date(pub_date)
                    if pd_dt:
                        dl = pd_dt + timedelta(days=20)
                        deadline = dl.strftime('%Y-%m-%d')
                results.append({
                    'id': str(hash(full_url)),
                    'title': title,
                    'url': full_url,
                    'pubDate': pub_date or '未知',
                    'deadline': deadline or '待确认',
                    'cancelReason': '',
                    'source': '北京市医保局',
                    'category': category,
                    'urgency': calc_urgency(deadline),
                    'fetchedAt': datetime.now().isoformat()
                })
        except Exception as e:
            logging.warning(f'医保局 {page_url} 失败: {e}')
    return results



def run_scraping():
    """执行全量抓取 — V1.0 宽进严出：关键词匹配后直接进入截止日期核实"""
    config = load_config()
    logging.info('=== 开始抓取招标数据 ===')
    update_scrape_progress(running=True, status='fetching', message='正在连接招标网站...', current_page='', source_name='', items_found=0, total_found=0)
    
    all_bids = []
    
    src_list = [
        ('scrape_ccgp', '中国政府采购网(北京)', scrape_ccgp),
        ('scrape_ggzy', '北京市公共资源交易平台', scrape_ggzy),
        ('scrape_laoganbu', '各区老干部局', scrape_laoganbu),
        ('scrape_junxiu', '军休所/退役军人事务局', scrape_junxiu),
        ('scrape_mzj', '北京市民政局', scrape_mzj),
        ('scrape_wjw', '北京市卫健委', scrape_wjw),
        ('scrape_ybj', '北京市医保局', scrape_ybj),
        ('scrape_cebpubservice', '中国采招网', scrape_cebpubservice),
        ('scrape_ccgp_search', '政府采购网(搜索)', scrape_ccgp_search),
    ]
    
    # ── 并行抓取 9 个数据源（4线程）──
    def _do_scrape(src_name, src_fn):
        try:
            items = src_fn(config)
            return src_name, items, None
        except Exception as e:
            return src_name, [], str(e)
    
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_do_scrape, n, f): n for _, n, f in src_list}
        for fut in as_completed(futures):
            name, items, err = fut.result()
            if err:
                logging.error(f'[进度] {name} 失败: {err}')
            else:
                all_bids.extend(items)
                logging.info(f'[进度] {name} 完成，新增 {len(items)} 条')
                update_scrape_progress(items_found=len(all_bids))
    
    # 去重
    seen = {}
    for b in all_bids:
        if b['url'] not in seen:
            seen[b['url']] = b
    unique = list(seen.values())
    logging.info(f'去重: {len(all_bids)} → {len(unique)} 条')
    
    # 过滤：2026年以前发布的公告不显示
    cutoff_2026 = datetime(2026, 1, 1)
    filtered_2026 = []
    old_count = 0
    for b in unique:
        pd = parse_date(b['pubDate'])
        if pd and pd < cutoff_2026:
            old_count += 1
            continue
        filtered_2026.append(b)
    if old_count > 0:
        logging.info(f'已过滤 {old_count} 条2026年前发布的数据')
    logging.info(f'年份过滤后: {len(filtered_2026)} 条')
    
    # ===== V1.0 宽进严出：不做招标特征词二次过滤 =====
    # 关键词匹配阶段已排除调查问卷/征求意见等，其余全部进入详情页截止日期核实
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    total = len(filtered_2026)
    logging.info(f'进入详情页提取截止日期: 共 {total} 条')
    
    def _verify_deadline(b):
        """单条验证：返回 (bid_dict, is_valid, detail_dl)"""
        detail_dl = extract_deadline_from_detail(b['url'])
        # 废标公告额外提取废标原因
        if b.get('category') == '废标公告':
            cancel_reason = extract_cancel_reason_from_detail(b['url'])
            if cancel_reason:
                b['cancelReason'] = cancel_reason
        is_valid = True
        if detail_dl:
            actual_dl = parse_date(detail_dl)
            if actual_dl and actual_dl < today:
                is_valid = False  # 过期丢弃
            elif actual_dl:
                b['deadline'] = detail_dl  # 有效 → 更新
        else:
            b['deadline'] = '待确认'  # 无法提取 → 保留标注
        return b, is_valid, detail_dl
    
    deadline_extracted = 0
    deadline_missing = 0
    expired_by_detail = 0
    valid = []
    
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_verify_deadline, b): b for b in filtered_2026}
        for fut in as_completed(futures):
            b, is_valid, detail_dl = fut.result()
            if detail_dl:
                deadline_extracted += 1
                if not is_valid:
                    expired_by_detail += 1
                    logging.info(f'[详情页过期] {b["title"][:40]} 截止 {detail_dl}')
                    continue
            else:
                deadline_missing += 1
            valid.append(b)
    
    logging.info(f'截止日期核实: 提取 {deadline_extracted} 条, 未找到 {deadline_missing} 条, 过期丢弃 {expired_by_detail} 条 → 最终 {len(valid)} 条')

    # 按截止日期排序
    valid.sort(key=lambda b: parse_date(b['deadline']) or datetime.max)
    
    save_bids(valid)
    logging.info(f'=== 抓取完成: {len(valid)} 条 ===')
    return valid

# ============ 邮件发送 ============
def build_email_html(bids):
    """生成邮件用的HTML"""
    cfg = load_config()
    total = len(bids)
    urgent = sum(1 for b in bids if b['urgency']['level'] == 'critical')
    warn = sum(1 for b in bids if b['urgency']['level'] == 'warning')
    
    rows = ''
    for b in bids[:50]:  # 邮件最多50条
        urg = b['urgency']
        color = '#fef2f2' if urg['level'] == 'critical' else ('#fffbeb' if urg['level'] == 'warning' else '#fff')
        cancel_html = f'<span style="color:#dc2626;font-size:12px">{b.get("cancelReason","")}</span>' if b.get('cancelReason') else ''
        rows += f'''<tr style="background:{color}">
            <td><a href="{b['url']}" target="_blank">{b['title']}</a></td>
            <td>{b['pubDate']}</td><td>{b['deadline']}</td>
            <td>{cancel_html}</td>
            <td>{'🔴' if urg['level']=='critical' else '🟠' if urg['level']=='warning' else ''}{urg['label']}</td>
            <td>{b['category']}</td><td>{b['source']}</td>
        </tr>'''
    
    return f'''<html><body>
    <h2>📋 北京市养老服务全业务线 · 招标监控日报</h2>
    <p>📅 {datetime.now().strftime("%Y-%m-%d")} | 📊 共 {total} 条 | 🔴 紧急 {urgent} 条 | 🟠 临近 {warn} 条</p>
    <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px">
        <tr style="background:#2563eb;color:#fff"><th>招标标题</th><th>发布时间</th><th>截止日期</th><th>废标原因</th><th>紧急度</th><th>分类</th><th>来源</th></tr>
        {rows}
    </table>
    <p style="color:#999;font-size:12px;margin-top:20px">本邮件由招标监控系统自动生成 | 查看完整报告请打开附件HTML</p>
</body></html>'''

def send_email_report(bids):
    """发送邮件报告"""
    cfg = load_config()
    ec = cfg['email']
    if not ec.get('smtp_user') or not ec.get('smtp_pass'):
        logging.warning('邮件未配置，跳过发送')
        return False, '邮件未配置'

    msg = MIMEMultipart('alternative')
    msg['Subject'] = Header(f'【招标监控日报】{datetime.now().strftime("%Y-%m-%d")} - 北京市养老服务全业务线招标汇总', 'utf-8')
    msg['From'] = ec['smtp_user']
    msg['To'] = ec['recipient']

    html = build_email_html(bids)
    msg.attach(MIMEText(html, 'html', 'utf-8'))

    try:
        with smtplib.SMTP_SSL(ec['smtp_host'], ec['smtp_port'], timeout=15) as smtp:
            smtp.login(ec['smtp_user'], ec['smtp_pass'])
            smtp.sendmail(ec['smtp_user'], [ec['recipient']], msg.as_string())
        logging.info(f'邮件发送成功 → {ec["recipient"]}')
        return True, '发送成功'
    except Exception as e:
        logging.error(f'邮件发送失败: {e}')
        return False, str(e)

# ============ 定时任务 ============
def scheduled_job():
    """每日定时任务"""
    logging.info('[SCHEDULER] 定时任务触发')
    bids = run_scraping()
    if bids:
        send_email_report(bids)

def start_scheduler():
    cfg = load_config()
    t = cfg.get('schedule_time', '09:00')
    schedule.every().day.at(t).do(scheduled_job)
    logging.info(f'定时任务已设置: 每日 {t}')
    
    def run_loop():
        while True:
            schedule.run_pending()
            time.sleep(30)
    
    threading.Thread(target=run_loop, daemon=True).start()

# ============ Flask API 路由 ============
@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.errorhandler(404)
def not_found(e):
    """所有未知路径回首页，防止白屏404"""
    return send_from_directory('.', 'index.html')

@app.route('/api/status')
def api_status():
    bids = load_bids()
    return jsonify({
        'status': 'ok',
        'bidCount': len(bids),
        'lastUpdate': bids[0]['fetchedAt'] if bids else None,
        'serverTime': datetime.now().isoformat()
    })

@app.route('/api/bids')
def api_bids():
    """返回全部招标数据"""
    bids = load_bids()
    # 重新计算紧急度
    for b in bids:
        b['urgency'] = calc_urgency(b['deadline'])
    return jsonify(bids)

@app.route('/api/fetch', methods=['POST'])
def api_fetch():
    """手动触发抓取（异步）"""
    with scrape_lock:
        if scrape_progress['running']:
            return jsonify({'success': False, 'message': '正在抓取中，请稍候'}), 429
        scrape_progress['running'] = True
        scrape_progress['status'] = 'fetching'
    
    def _run():
        try:
            bids = run_scraping()
            update_scrape_progress(running=False, status='completed', message=f'抓取完成，共 {len(bids)} 条')
        except Exception as e:
            update_scrape_progress(running=False, status='error', message=str(e))
    
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({'success': True, 'message': '抓取已启动'})

@app.route('/api/fetch-status', methods=['GET'])
def api_fetch_status():
    with scrape_lock:
        return jsonify(dict(scrape_progress))

@app.route('/api/fetch', methods=['DELETE'])
def api_fetch_cancel():
    with scrape_lock:
        scrape_progress['running'] = False
        scrape_progress['status'] = 'idle'
        scrape_progress['message'] = '已取消'
    return jsonify({'success': True, 'message': '已取消抓取'})

@app.route('/api/email', methods=['POST'])
def api_email():
    """手动发送邮件"""
    bids = load_bids()
    if not bids:
        return jsonify({'success': False, 'message': '无数据可发送'}), 400
    ok, msg = send_email_report(bids)
    return jsonify({'success': ok, 'message': msg})

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """配置管理"""
    if request.method == 'GET':
        return jsonify(load_config())
    else:
        cfg = request.get_json()
        if cfg:
            save_config(cfg)
        return jsonify({'success': True})

# ============ 启动 ============
if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 保留已有数据（不过滤年份和过期）
    bids = load_bids()
    
    # 无数据则首次抓取
    if not bids:
        logging.info('无缓存数据，执行首次抓取...')
        try:
            run_scraping()
        except Exception as e:
            logging.warning(f'首次抓取失败（网络可能不可用）: {e}')
        finally:
            update_scrape_progress(running=False, status='completed', message='启动抓取完成')
    
    # 启动定时任务
    start_scheduler()
    
    port = int(os.environ.get('PORT', 5000))
    logging.info(f'[OK] 招标监控系统启动 -> http://0.0.0.0:{port}')
    app.run(host='0.0.0.0', port=port, debug=False)
