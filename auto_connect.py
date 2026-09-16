#!/usr/bin/env python3
import argparse
import re
import socket
import time
from html import unescape

import requests


# ==================== 配置区 ====================
USER_ID = "24820274"
PASSWORD = "Wuting0106.."

PREFER_TELECOM = False
TELECOM_USER_ID = ""
TELECOM_PASSWORD = ""

# 普通校园网服务器建议保持 False。
ENABLE_AUTO_SWITCH = False

MAX_CONSECUTIVE_FAILURES = 2
CHECK_INTERVAL_SECONDS = 10
HEALTH_LOG_INTERVAL_SECONDS = 120

PORTAL_BASE_URL = "http://10.10.9.9"

# 仅在公网 TCP 不通、需要重新认证时使用。
# 不能使用 HTTPS；未认证时校园网无法安全地劫持 HTTPS。
QUERY_URLS = (
    "http://www.baidu.com/",
    "http://neverssl.com/",
)

# 任一目标可建立 TCP 连接，即视为公网连接可用。
TCP_CHECK_TARGETS = (
    ("www.baidu.com", 443),
    ("www.qq.com", 443),
)
# ================================================


class ShuNetwork:
    def __init__(self):
        self.user_id = USER_ID
        self.password = PASSWORD
        self.telecom_user_id = TELECOM_USER_ID
        self.telecom_password = TELECOM_PASSWORD
        self.use_telecom = PREFER_TELECOM

        self.user_index = ""
        self.consecutive_failures = 0
        self.last_health_log_time = 0.0

        self.session = requests.Session()
        # 校园网网关是内网地址，不能经由失效的本地代理访问。
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/111.0.0.0 Safari/537.36"
                )
            }
        )

    @staticmethod
    def log(message):
        print(
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())} {message}",
            flush=True,
        )

    def current_service_name(self):
        return "中国电信" if self.use_telecom else "校园网"

    def check_internet_connection(self):
        """直接测试公网 TCP，不使用系统或浏览器代理。"""
        for host, port in TCP_CHECK_TARGETS:
            try:
                with socket.create_connection((host, port), timeout=5):
                    return True
            except OSError:
                continue
        return False

    def get_login_status(self):
        """
        返回：
          1: 网关明确显示已认证
          2: 网关明确显示未认证
          3: 网关返回页面配置，状态无法可靠判断
          4: 认证网关不可达或请求异常
        """
        try:
            response = self.session.post(
                f"{PORTAL_BASE_URL}/eportal/InterFace.do?method=getOnlineUserInfo",
                timeout=(10, 10),
                allow_redirects=False,
            )
            response.encoding = "utf-8"

            if response.status_code in (301, 302, 303, 307, 308):
                return 2

            if response.status_code != 200:
                self.log(f"校园网认证服务异常：HTTP {response.status_code}")
                return 4

            result = response.json()

            if result.get("result") == "success":
                self.user_index = result.get("userIndex", "")

                service = result.get("service", "")
                if service == "校园网":
                    self.use_telecom = False
                elif service == "中国电信":
                    self.use_telecom = True

                return 1

            if result.get("result") == "fail":
                return 2

            # 此网关可能在设备已在线时仍返回 portalIp/serviceList。
            # 因此不能把它直接判成“未认证”。
            if "portalIp" in result or "serviceList" in result:
                return 3

            self.log(
                "认证状态响应格式未知，字段为：" + ", ".join(result.keys())
            )
            return 3

        except (requests.RequestException, ValueError) as error:
            self.log(f"获取登录状态异常：{error}")
            return 4

    def get_login_query_string(self):
        """从未认证状态下的跳转页提取动态 queryString。"""
        errors = []

        for url in QUERY_URLS:
            try:
                response = self.session.get(
                    url,
                    allow_redirects=False,
                    timeout=(5, 10),
                )
                response.encoding = "utf-8"

                candidates = (
                    response.headers.get("Location", ""),
                    response.text,
                )
                for text in candidates:
                    match = re.search(r'index\.jsp\?([^\'"<\s]+)', text)
                    if match:
                        return unescape(match.group(1))

                errors.append(f"{url}: 未发现 index.jsp?")
            except requests.RequestException as error:
                errors.append(f"{url}: {error}")

        raise RuntimeError("无法提取 queryString；" + "；".join(errors))

    def connect(self):
        """尝试认证；不会主动注销已存在的在线会话。"""
        user_id = self.telecom_user_id if self.use_telecom else self.user_id
        password = self.telecom_password if self.use_telecom else self.password

        if not user_id or not password:
            raise RuntimeError(f"{self.current_service_name()}账号或密码未配置")

        data = {
            "userId": user_id,
            "password": password,
            "passwordEncrypt": "false",
            "queryString": self.get_login_query_string(),
            "service": (
                "%E4%B8%AD%E5%9B%BD%E7%94%B5%E4%BF%A1"
                if self.use_telecom
                else "shu"
            ),
            "operatorPwd": "",
            "operatorUserId": "",
            "validcode": "",
        }

        response = self.session.post(
            f"{PORTAL_BASE_URL}/eportal/InterFace.do?method=login",
            data=data,
            timeout=(15, 15),
            allow_redirects=False,
        )
        response.encoding = "utf-8"

        if response.status_code != 200:
            raise RuntimeError(f"登录接口返回 HTTP {response.status_code}")

        result = response.json()
        self.user_index = result.get("userIndex", "")
        message = result.get("message", "")
        result_name = result.get("result", "unknown")

        self.log(
            f"登录至 {self.current_service_name()}：{result_name} {message}"
        )

        # 网关提示“当前设备已存在在线用户”也表示不应继续重复登录。
        return result_name == "success" or "当前设备已存在在线用户" in message

    def try_login(self):
        try:
            self.log(f"尝试认证 {self.current_service_name()}")
            return self.connect()
        except Exception as error:
            self.log(f"认证失败：{error}")
            return False

    def run(self):
        self.log("校园网自动认证服务已启动。按 Ctrl+C 停止。")

        while True:
            if self.check_internet_connection():
                self.consecutive_failures = 0

                now = time.time()
                if now - self.last_health_log_time >= HEALTH_LOG_INTERVAL_SECONDS:
                    self.log("网络正常，公网 TCP 连接可用")
                    self.last_health_log_time = now
            else:
                self.consecutive_failures += 1
                self.log(
                    "公网 TCP 检测失败 "
                    f"({self.consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})"
                )

                if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    status = self.get_login_status()

                    if status == 1:
                        self.log(
                            "网关显示已认证但公网 TCP 不通；"
                            "为避免误下线，本次不主动注销"
                        )
                    elif status in (2, 3):
                        self.log("网关未明确显示在线，尝试登录但不注销当前会话")
                        self.try_login()
                    else:
                        self.log("认证网关不可达，暂不执行登录")

                    self.consecutive_failures = 0

            time.sleep(CHECK_INTERVAL_SECONDS)


def parse_args():
    parser = argparse.ArgumentParser(description="上海大学校园网自动认证")
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="仅检查一次公网 TCP 连通性，不登录、不下线",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    shu = ShuNetwork()

    if args.check_once:
        if shu.check_internet_connection():
            shu.log("公网 TCP 连接正常：当前网络已放行")
            raise SystemExit(0)

        status = shu.get_login_status()
        shu.log("公网 TCP 连接不可用")
        if status == 1:
            shu.log("网关状态：已认证")
        elif status == 2:
            shu.log("网关状态：未认证")
        elif status == 3:
            shu.log("网关状态：页面配置响应，无法可靠判断")
        else:
            shu.log("网关状态：不可达或请求异常")
        raise SystemExit(1)

    if USER_ID == "填写你的学号" or PASSWORD == "填写你的校园网密码":
        raise RuntimeError("请先在配置区填写 USER_ID 和 PASSWORD")

    try:
        shu.run()
    except KeyboardInterrupt:
        shu.log("校园网自动认证服务已停止")
