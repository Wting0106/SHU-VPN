#!/usr/bin/env python3
import argparse
import re
import socket
import time

import requests


# ==================== 配置区 ====================

# 校园网账号
USER_ID = "24820274"
PASSWORD = "Wuting0106.."

# 是否优先使用中国电信认证。普通校园网请保持 False。
PREFER_TELECOM = False

# 仅当你确实有电信宽带账号时再填写
TELECOM_USER_ID = ""
TELECOM_PASSWORD = ""

# 是否在“当前已认证但公网不可用”时自动切换校园网/电信。
# 建议服务器保持 False，避免网络短暂抖动导致切错账号。
ENABLE_AUTO_SWITCH = False

# 连续失败次数达到该值后，尝试重新认证
MAX_CONSECUTIVE_FAILURES = 2

# 网络检测间隔（秒）
CHECK_INTERVAL_SECONDS = 10

# 每隔多久打印一次正常状态（秒）
HEALTH_LOG_INTERVAL_SECONDS = 120

# 校园网认证地址
PORTAL_BASE_URL = "http://10.10.9.9"
QUERY_URL = "http://123.123.123.123/"

# 用于判断“公网是否真的可用”
CONNECTIVITY_URL = "https://www.baidu.com"

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
        # 校园网认证地址是内网地址，不能经过系统或环境变量中的 HTTP 代理。
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/111.0.0.0 Safari/537.36"
            )
        })

    @staticmethod
    def log(message):
        print(
            f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())} {message}",
            flush=True,
        )

    def current_service_name(self):
        return "中国电信" if self.use_telecom else "校园网"

    def check_internet_connection(self):
        """检查 HTTPS 网页是否可访问，而非仅检查 DNS 端口。"""
        try:
            response = self.session.get(
                CONNECTIVITY_URL,
                timeout=(3, 6),
                allow_redirects=True,
            )
            return response.status_code < 400
        except requests.RequestException:
            return False

    def get_login_status(self):
        """
        返回：
          1: 已认证
          2: 未认证
          3: 响应异常
          4: 网关不可达/请求异常
        """
        try:
            response = self.session.post(
                f"{PORTAL_BASE_URL}/eportal/InterFace.do?method=getOnlineUserInfo",
                timeout=(10, 10),
            )
            response.encoding = "utf-8"

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

            self.log(f"登录状态响应异常：{response.text}")
            return 3

        except (requests.RequestException, ValueError) as error:
            self.log(f"获取登录状态异常：{error}")
            return 4

    def get_login_query_string(self):
        """从认证跳转页中提取 queryString。"""
        response = self.session.get(
            QUERY_URL,
            allow_redirects=False,
            timeout=(10, 10),
        )
        response.encoding = "utf-8"

        match = re.search(r"index\.jsp\?([^'\"<\s]+)", response.text)
        if not match:
            raise RuntimeError("无法从认证跳转页提取 queryString")

        return match.group(1)

    def connect(self):
        """使用当前选中的校园网或电信账号认证。"""
        user_id = self.telecom_user_id if self.use_telecom else self.user_id
        password = self.telecom_password if self.use_telecom else self.password

        if not user_id or not password:
            raise RuntimeError(f"{self.current_service_name()}的账号或密码未配置")

        data = {
            "userId": user_id,
            "password": password,
            "passwordEncrypt": "false",
            "queryString": self.get_login_query_string(),
            "service": "shu" if not self.use_telecom else "%E4%B8%AD%E5%9B%BD%E7%94%B5%E4%BF%A1",
            "operatorPwd": "",
            "operatorUserId": "",
            "validcode": "",
        }

        response = self.session.post(
            f"{PORTAL_BASE_URL}/eportal/InterFace.do?method=login",
            data=data,
            timeout=(15, 15),
        )
        response.encoding = "utf-8"
        result = response.json()

        self.user_index = result.get("userIndex", "")
        self.log(
            f"登录至 {self.current_service_name()}："
            f"{result.get('result', 'unknown')} "
            f"{result.get('message', '')}"
        )

        return result.get("result") == "success"

    def disconnect(self):
        """主动下线。仅用于手动测试或重新认证。"""
        if not self.user_index:
            status = self.get_login_status()
            if status != 1:
                self.log("当前未获取到有效 userIndex，无需下线")
                return False

        response = self.session.post(
            f"{PORTAL_BASE_URL}/eportal/InterFace.do?method=logout",
            data={"userIndex": self.user_index},
            timeout=(15, 15),
        )
        response.encoding = "utf-8"
        result = response.json()

        self.log(
            f"离线状态：{result.get('result', 'unknown')} "
            f"{result.get('message', '')}"
        )
        return result.get("result") == "success"

    def reconnect(self, switch_service=False):
        """重新认证；默认保持当前网络类型。"""
        if switch_service:
            self.use_telecom = not self.use_telecom
            self.log(f"切换至 {self.current_service_name()} 后重连")
        else:
            self.log(f"尝试重新认证 {self.current_service_name()}")

        try:
            self.disconnect()
        except Exception as error:
            self.log(f"下线请求失败，将直接尝试认证：{error}")

        time.sleep(1)

        try:
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
                    status = self.get_login_status()
                    if status == 1:
                        self.log(f"网络正常，当前认证：{self.current_service_name()}")
                    else:
                        self.log(f"公网可访问，但认证状态查询返回：{status}")
                    self.last_health_log_time = now

            else:
                self.consecutive_failures += 1
                self.log(
                    f"公网检测失败 "
                    f"({self.consecutive_failures}/{MAX_CONSECUTIVE_FAILURES})"
                )

                if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    status = self.get_login_status()

                    if status == 2:
                        self.log("检测到当前未认证，开始登录")
                        self.reconnect()

                    elif status == 1:
                        can_switch = (
                            ENABLE_AUTO_SWITCH
                            and bool(self.telecom_user_id)
                            and bool(self.telecom_password)
                        )
                        self.log("当前已认证但公网不可达，尝试重新认证")
                        self.reconnect(switch_service=can_switch)

                    else:
                        self.log(f"认证网关状态异常，暂不执行登录：{status}")

                    self.consecutive_failures = 0

            time.sleep(CHECK_INTERVAL_SECONDS)


def parse_args():
    parser = argparse.ArgumentParser(description="上海大学校园网自动认证")
    parser.add_argument(
        "--check-once",
        action="store_true",
        help="仅检查一次公网和认证状态，不执行登录、下线或重连",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    shu = ShuNetwork()

    if args.check_once:
        internet_ok = shu.check_internet_connection()
        status = shu.get_login_status()
        shu.log(f"公网访问：{'正常' if internet_ok else '不可用'}")
        if status == 1:
            shu.log(f"认证状态：已认证（{shu.current_service_name()}）")
        elif status == 2:
            shu.log("认证状态：未认证")
        else:
            shu.log(f"认证状态：查询异常（状态码 {status}）")
        raise SystemExit(0 if status in (1, 2) else 1)

    if USER_ID == "你的学号" or PASSWORD == "你的校园网密码":
        raise RuntimeError("请先在配置区填写 USER_ID 和 PASSWORD")

    try:
        shu.run()
    except KeyboardInterrupt:
        shu.log("校园网自动认证服务已停止")
