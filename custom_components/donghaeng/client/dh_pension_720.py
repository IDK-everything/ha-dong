import datetime
import json
import logging
import base64
import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

import aiohttp
import yarl
from Crypto.Cipher import AES
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA256
from Crypto.Random import get_random_bytes

from bs4 import BeautifulSoup as BS
from .dh_lottery_client import DhLotteryClient, DhLotteryError

_LOGGER = logging.getLogger(__name__)


class DhPension720Error(DhLotteryError):
    """DH Pension 720+ 예외 클래스입니다."""


@dataclass
class DhPension720Game:
    """연금복권 개별 게임 정보를 나타내는 데이터 클래스입니다."""
    group: str       # 조 (예: 1조, 2조, ...)
    numbers: str     # 6자리 번호 (예: 123456)
    mode: str = "자동"
    status: str = "미추첨"  # 당첨 여부/등수
    rank: int = -1         # 등수 (-1: 미추첨, 0: 낙첨, 1~7: 등수)


@dataclass
class DhPension720BuyData:
    """연금복권 구매 결과를 나타내는 데이터 클래스입니다."""
    round_no: int
    order_no: str
    issue_dt: str
    games: List[DhPension720Game] = field(default_factory=list)
    failed_candidates: List[str] = field(default_factory=list)
    success_candidate: Optional[str] = None

    def to_dict(self) -> Dict:
        """데이터를 사전 형식으로 변환합니다."""
        return {
            "round_no": self.round_no,
            "order_no": self.order_no,
            "issue_dt": self.issue_dt,
            "games": [game.__dict__ for game in self.games],
            "failed_candidates": self.failed_candidates,
            "success_candidate": self.success_candidate,
        }


@dataclass
class DhPension720BuyHistoryData:
    """연금복권 구매 내역을 나타내는 데이터 클래스입니다."""
    round_no: int
    order_no: str
    result: str
    games: List[DhPension720Game] = field(default_factory=list)


class DhPension720:
    """동행복권 연금복권 720+를 구매 및 조회하는 클래스입니다."""

    keySize = 128
    iterationCount = 1000
    BlockSize = 16

    _pad = lambda self, s: s + (self.BlockSize - len(s) % self.BlockSize) * chr(self.BlockSize - len(s) % self.BlockSize)
    _unpad = lambda self, s: s[:-ord(s[len(s) - 1:])]

    _REQ_HEADERS = {
        "Connection": "keep-alive",
        "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "Origin": "https://el.dhlottery.co.kr",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.9",
        "Referer": "https://el.dhlottery.co.kr/game/pension720/game.jsp",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "sec-ch-ua-platform": '"Windows"',
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "ko,ko-KR;q=0.9,en-US;q=0.8,en;q=0.7",
        "X-Requested-With": "XMLHttpRequest",
    }

    def __init__(self, client: DhLotteryClient):
        """DhPension720 클래스를 초기화합니다."""
        self.client = client

    async def _async_ensure_el_session(self) -> str:
        """el.dhlottery.co.kr 세션을 확인하고 JSESSIONID/DHJSESSIONID를 반환합니다."""
        # 1. www.dhlottery.co.kr의 DHJSESSIONID 또는 JSESSIONID를 먼저 조회합니다.
        www_cookies = self.client.session.cookie_jar.filter_cookies(yarl.URL("https://www.dhlottery.co.kr"))
        www_jsessionid = www_cookies.get("DHJSESSIONID") or www_cookies.get("JSESSIONID")

        # 2. 만약 필터로 직접 조회가 안 된다면 전체 쿠키에서 탐색합니다.
        if not www_jsessionid:
            for cookie in self.client.session.cookie_jar:
                if cookie.key.upper() in ("DHJSESSIONID", "JSESSIONID"):
                    www_jsessionid = cookie
                    break

        # 3. 여전히 없으면, 로그인 세션이 만료되거나 지워진 것이므로 강제 로그인을 시도합니다.
        if not www_jsessionid:
            _LOGGER.info("동행복권 로그인 세션이 없습니다. 강제 재로그인을 진행합니다.")
            try:
                await self.client.async_login()
                # 로그인 성공 후 다시 쿠키를 읽어옵니다.
                www_cookies = self.client.session.cookie_jar.filter_cookies(yarl.URL("https://www.dhlottery.co.kr"))
                www_jsessionid = www_cookies.get("DHJSESSIONID") or www_cookies.get("JSESSIONID")
                if not www_jsessionid:
                    for cookie in self.client.session.cookie_jar:
                        if cookie.key.upper() in ("DHJSESSIONID", "JSESSIONID"):
                            www_jsessionid = cookie
                            break
            except Exception as ex:
                raise DhPension720Error(f"❗재로그인 수행 실패: {ex}")

        if not www_jsessionid:
            raise DhPension720Error("❗로그인 세션(JSESSIONID/DHJSESSIONID)을 획득할 수 없습니다. 다시 로그인해 주세요.")

        www_jsessionid_value = www_jsessionid.value
        www_jsessionid_key = www_jsessionid.key

        # 4. www.dhlottery.co.kr의 세션 쿠키를 el.dhlottery.co.kr 도메인에도 명시적으로 심어줍니다.
        # aiohttp의 엄격한 쿠키 분리 정책으로 인해 이 과정이 필수적입니다.
        self.client.session.cookie_jar.update_cookies(
            {www_jsessionid_key: www_jsessionid_value},
            response_url=yarl.URL("https://el.dhlottery.co.kr")
        )

        # 5. el.dhlottery.co.kr 페이지를 요청하여 세션 활성화 및 확인 과정을 거칩니다.
        try:
            await self.client.session.get(
                "https://el.dhlottery.co.kr/game/pension720/game.jsp",
                headers={
                    "User-Agent": self.client.session.headers.get("User-Agent", "Mozilla/5.0"),
                    "Referer": "https://www.dhlottery.co.kr/",
                }
            )
        except Exception as ex:
            _LOGGER.warning(f"el.dhlottery.co.kr 세션 활성화 GET 요청 실패: {ex}")

        # 6. 최종적으로 el.dhlottery.co.kr 도메인용으로 할당된 세션 쿠키값을 반환합니다.
        el_cookies = self.client.session.cookie_jar.filter_cookies(yarl.URL("https://el.dhlottery.co.kr"))
        el_jsessionid = el_cookies.get(www_jsessionid_key)

        if not el_jsessionid:
            return www_jsessionid_value

        return el_jsessionid.value

    async def async_get_latest_round_no(self) -> int:
        """최신 연금복권 회차 번호를 가져옵니다."""
        try:
            resp = await self.client.session.get(
                "https://www.dhlottery.co.kr/common.do?method=main",
                headers=self._REQ_HEADERS
            )
            html = await resp.text()
            soup = BS(html, "html.parser")
            found = soup.find("strong", id="drwNo720")
            if found:
                return int(found.text)
        except Exception as ex:
            _LOGGER.warning(f"메인페이지에서 drwNo720 조회 실패: {ex}. 날짜 기준 자동 계산으로 대체합니다.")

        base_date = datetime.datetime(2024, 12, 26)
        base_round = 244
        
        today = datetime.datetime.today().replace(hour=0, minute=0, second=0, microsecond=0)
        
        days_ahead = (3 - today.weekday()) % 7
        next_thursday = today + datetime.timedelta(days=days_ahead)
        
        weeks = (next_thursday - base_date).days // 7
        
        now = datetime.datetime.now()
        current_thursday_draw = now.replace(hour=19, minute=5, second=0, microsecond=0)
        
        calculated_round = base_round + weeks
        if now.weekday() == 3 and now < current_thursday_draw:
            return calculated_round - 1
            
        return calculated_round

    async def _async_do_order_request(
        self, win720_round: str, num: str, key_code: str, headers: dict
    ) -> tuple[str, str]:
        """지정된 번호로 주문(예약)을 요청합니다."""
        order_payload = f"ROUND={win720_round}&round={win720_round}&LT_EPSD={win720_round}&AUTO_SEL_SET=SA&SEL_CLASS=&SEL_NO={num}&BUY_TYPE=M&BUY_CNT=5"
        enc_order_payload = self._encText(order_payload, key_code)

        try:
            resp = await self.client.session.post(
                url="https://el.dhlottery.co.kr/makeOrderNo.do",
                headers=headers,
                data={"q": enc_order_payload}
            )
            resp_text = await resp.text()
            order_ret = json.loads(resp_text)
            q_val = order_ret.get('q')
            if not q_val:
                 raise DhPension720Error(f"makeOrderNo 응답이 올바르지 않습니다: {resp_text[:100]}")
        except Exception as ex:
            raise DhPension720Error(f"주문 예약 통신 중 오류가 발생했습니다: {ex}")

        decrypted_order = self._decText(q_val, key_code)
        try:
            parsed_order = json.loads(decrypted_order)
            if 'orderNo' not in parsed_order or 'orderDate' not in parsed_order:
                result_msg = parsed_order.get("resultMsg", "예약 실패")
                raise DhPension720Error(result_msg)
            return parsed_order['orderNo'], parsed_order['orderDate']
        except Exception as ex:
            raise DhPension720Error(f"주문 예약 결과 처리 실패 ({ex}): {decrypted_order[:200]}...")

    async def async_buy(self, candidates: Optional[List[str]] = None) -> DhPension720BuyData:
        """연금복권을 1세트(5장) 수동 후보군 순차 구매 또는 자동으로 구매합니다."""
        _LOGGER.info("연금복권 구매 시작")
        
        now = datetime.datetime.now()
        if now.weekday() == 3 and 17 <= now.hour < 20:
             raise DhPension720Error("❗목요일 오후 5시부터 8시까지는 판매 정지 시간입니다.")
        if 0 <= now.hour < 6:
             raise DhPension720Error("❗구매 가능 시간이 아닙니다. (매일 6시부터 24시까지 구매 가능)")

        balance = await self.client.async_get_balance()
        if balance.purchase_available < 5000:
            raise DhPension720Error(f"❗예치금이 부족합니다. (예치금: {balance.purchase_available}원 / 필요금액: 5,000원)")

        jsessionid = await self._async_ensure_el_session()
        keyCode = jsessionid

        latest_round = await self.async_get_latest_round_no()
        target_round = latest_round + 1
        win720_round = str(latest_round)

        headers = self.client.session.headers.copy()
        headers.update(self._REQ_HEADERS)

        if candidates is None:
            candidates = ["810212", "810410", "120911", "150402"]

        reserved_num = None
        orderNo = None
        orderDate = None
        failed_candidates = []
        success_candidate = None

        for num in candidates:
            _LOGGER.info(f"연금복권 수동 후보 번호 {num} 예약 시도 중...")
            try:
                orderNo, orderDate = await self._async_do_order_request(
                    win720_round=win720_round,
                    num=num,
                    key_code=keyCode,
                    headers=headers
                )
                reserved_num = num
                success_candidate = num
                _LOGGER.info(f"수동 후보 번호 {num} 예약 성공! (주문번호: {orderNo})")
                break
            except Exception as ex:
                failed_candidates.append(num)
                _LOGGER.warning(f"수동 후보 번호 {num} 예약 실패 (다음 후보 시도): {ex}")
                continue

        if not reserved_num:
            _LOGGER.info("모든 수동 후보 번호 예약에 실패하여 자동 번호로 구매를 진행합니다.")
            payload = f"ROUND={win720_round}&round={win720_round}&LT_EPSD={win720_round}&SEL_NO=&BUY_CNT=&AUTO_SEL_SET=SA&SEL_CLASS=&BUY_TYPE=A&ACCS_TYPE=01"
            enc_payload = self._encText(payload, keyCode)
            
            try:
                resp = await self.client.session.post(
                    url="https://el.dhlottery.co.kr/makeAutoNo.do",
                    headers=headers,
                    data={"q": enc_payload}
                )
                resp_text = await resp.text()
                make_auto_ret = json.loads(resp_text)
                q_val = make_auto_ret.get('q')
                if not q_val:
                    raise DhPension720Error(f"makeAutoNo 응답이 올바르지 않습니다: {resp_text[:100]}")
            except Exception as ex:
                raise DhPension720Error(f"번호 생성(makeAutoNo) 중 오류가 발생했습니다: {ex}")

            decrypted = self._decText(q_val, keyCode)
            if "resultMsg" in decrypted and ":" in decrypted:
                 decrypted = re.sub(r'("resultMsg":\s*)([^",}]*)([,}])', r'\1"\2"\3', decrypted)

            try:
                parsed_ret = json.loads(decrypted)
            except Exception as ex:
                raise DhPension720Error(f"번호 생성 응답 복호화 실패: {decrypted[:200]}...")

            extracted_num = parsed_ret.get("selLotNo", "")
            if not extracted_num:
                result_msg = parsed_ret.get("resultMsg", "알 수 없는 오류")
                raise DhPension720Error(f"연금복권 번호 획득 실패 (사유: {result_msg})")

            _LOGGER.info(f"자동 생성 번호 {extracted_num} 예약 시도 중...")
            try:
                orderNo, orderDate = await self._async_do_order_request(
                    win720_round=win720_round,
                    num=extracted_num,
                    key_code=keyCode,
                    headers=headers
                )
                reserved_num = extracted_num
            except Exception as ex:
                raise DhPension720Error(f"자동 번호 예약 생성 실패: {ex}")

        # 결제 진행
        buy_no_str = "".join([f"{i}{reserved_num}%2C" for i in range(1, 6)])[:-3]
        conn_payload = (
            f"ROUND={win720_round}&FLAG=&BUY_KIND=01&BUY_NO={buy_no_str}&BUY_CNT=5"
            f"&BUY_SET_TYPE=SA%2CSA%2CSA%2CSA%2CSA&BUY_TYPE=A%2CA%2CA%2CA%2CA%2C&CS_TYPE=01"
            f"&orderNo={orderNo}&orderDate={orderDate}&TRANSACTION_ID=&WIN_DATE="
            f"&USER_ID={self.client.username}&PAY_TYPE=&resultErrorCode=&resultErrorMsg=&resultOrderNo="
            f"&WORKING_FLAG=true&NUM_CHANGE_TYPE=&auto_process=N&set_type=SA&classnum=&selnum=&buytype=M"
            f"&num1=&num2=&num3=&num4=&num5=&num6=&DSEC=34&CLOSE_DATE=&verifyYN=N"
            f"&curdeposit=&curpay=5000&DROUND={win720_round}&DSEC=0&CLOSE_DATE=&verifyYN=N&lotto720_radio_group=on"
        )
        enc_conn_payload = self._encText(conn_payload, keyCode)

        try:
            resp = await self.client.session.post(
                url="https://el.dhlottery.co.kr/connPro.do",
                headers=headers,
                data={"q": enc_conn_payload}
            )
            resp_text = await resp.text()
            conn_ret = json.loads(resp_text)
            q_val = conn_ret.get('q')
            if not q_val:
                 raise DhPension720Error(f"connPro 응답이 올바르지 않습니다: {resp_text[:100]}")
        except Exception as ex:
            raise DhPension720Error(f"결제 진행(connPro) 중 오류가 발생했습니다: {ex}")

        decrypted_conn = self._decText(q_val, keyCode)
        try:
            parsed_conn = json.loads(decrypted_conn)
        except Exception as ex:
            raise DhPension720Error(f"결제 결과 복호화 실패: {decrypted_conn[:200]}...")

        result = parsed_conn.get("result", {})
        result_msg = result.get("resultMsg", "FAILURE")
        if result_msg.upper() != "SUCCESS":
            error_msg = result.get("resultErrorMsg", "결제에 실패했습니다.")
            raise DhPension720Error(f"❗연금복권 결제 실패: {error_msg}")

        games = []
        is_manual = reserved_num in candidates
        for i in range(1, 6):
            games.append(
                DhPension720Game(
                    group=f"{i}조",
                    numbers=reserved_num,
                    mode="수동" if is_manual else "자동",
                    status="미추첨",
                    rank=-1
                )
            )

        buy_data = DhPension720BuyData(
            round_no=target_round,
            order_no=orderNo,
            issue_dt=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            games=games,
            failed_candidates=failed_candidates,
            success_candidate=success_candidate
        )

        _LOGGER.info(f"연금복권 {target_round}회 1세트 구매 완료 (수동여부: {is_manual}, 주문번호: {orderNo})")
        return buy_data

    async def async_get_buy_history_this_week(self) -> List[DhPension720BuyHistoryData]:
        """최근 1주일간의 연금복권 구매 내역을 조회합니다."""
        try:
            results = await self.client.async_get_buy_list("LP72")
            items: List[DhPension720BuyHistoryData] = []
            
            for result in results:
                order_no = result.get("ntslOrdrNo")
                lt_wn_result = result.get("ltWnResult", "미추첨")
                round_no = result.get("ltEpsd")
                
                detail_url = "mypage/lottery720select.do"
                detail_params = {
                    "ntslOrdrNo": order_no,
                    "_": int(datetime.datetime.now().timestamp() * 1000)
                }
                
                detail_data = await self.client.async_get_with_login(detail_url, params=detail_params)
                if isinstance(detail_data, dict):
                    detail_list = detail_data.get("list", [])
                else:
                    detail_list = []
                    
                games: List[DhPension720Game] = []
                for d_item in detail_list:
                    info_cn = d_item.get("ltGmInfoCn", "")
                    rank_raw = d_item.get("wnRnk")
                    
                    try:
                        rank = int(rank_raw) if rank_raw is not None else 0
                    except (ValueError, TypeError):
                        rank = 0
                        
                    status = "미추첨" if lt_wn_result == "미추첨" else ("낙첨" if rank == 0 else f"{rank}등 당첨")
                    
                    group = "?"
                    number = info_cn
                    if ":" in info_cn:
                        parts = info_cn.split(":")
                        group = f"{parts[0]}조"
                        number = parts[1]
                        
                    games.append(
                        DhPension720Game(
                            group=group,
                            numbers=number,
                            mode="자동",
                            status=status,
                            rank=-1 if lt_wn_result == "미추첨" else rank
                        )
                    )
                
                items.append(
                    DhPension720BuyHistoryData(
                        round_no=round_no,
                        order_no=order_no,
                        result=lt_wn_result,
                        games=games
                    )
                )
                
                if len(items) >= 2:
                    break
                    
            return items
            
        except Exception as ex:
            _LOGGER.error(f"연금복권 최근 구매내역 조회 실패: {ex}")
            return []

    def _encText(self, plainText: str, key_code: str) -> str:
        encSalt = get_random_bytes(32)
        encIV = get_random_bytes(16)
        passPhrase = key_code[:32]
        encKey = PBKDF2(passPhrase, encSalt, self.BlockSize, count=self.iterationCount, hmac_hash_module=SHA256)
        aes = AES.new(encKey, AES.MODE_CBC, encIV)

        padded_text = self._pad(plainText).encode('utf-8')
        encrypted = aes.encrypt(padded_text)

        return "{}{}{}".format(bytes.hex(encSalt), bytes.hex(encIV), base64.b64encode(encrypted).decode('utf-8'))

    def _decText(self, encText: str, key_code: str) -> str:
        decSalt = bytes.fromhex(encText[0:64])
        decIv = bytes.fromhex(encText[64:96])
        cryptText = encText[96:]
        passPhrase = key_code[:32]
        decKey = PBKDF2(passPhrase, decSalt, self.BlockSize, count=self.iterationCount, hmac_hash_module=SHA256)

        aes = AES.new(decKey, AES.MODE_CBC, decIv)
        decrypted_bytes = self._unpad(aes.decrypt(base64.b64decode(cryptText)))
        
        try:
            return decrypted_bytes.decode('utf-8')
        except UnicodeDecodeError:
            try:
                return decrypted_bytes.decode('euc-kr')
            except UnicodeDecodeError:
                return f'{{"resultMsg": "Decryption Failed (Raw: {decrypted_bytes.hex()[:20]}...)"}}'
