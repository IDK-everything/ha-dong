import datetime
import logging
from dataclasses import dataclass
from typing import Any, Optional, List, Callable, Awaitable

import async_timeout

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .client.dh_lottery_client import (
    DhLotteryError,
    DhLotteryClient,
    DhLotteryBalanceData,
)
from .client.dh_lotto_645 import DhLotto645
from .client.dh_pension_720 import DhPension720, DhPension720Game, DhPension720BuyHistoryData
from .const import (
    CONF_LOTTO_645,
    CONF_PENSION_720,
    COORDINATOR_UPDATE_INTERVAL,
    DRAW_RESULT_CHECK_INTERVAL,
    LOTTO_645_UPDATE_INTERVAL,
    LOTTERY_ACCUMULATED_PRIZE_UPDATE_INTERVAL,
    LOTTERY_BALANCE_UPDATE_INTERVAL,
    PENSION_720_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(order=True)
class DhLotto645BuyData:
    """로또 구매 내역을 나타내는 데이터 클래스입니다."""

    round_no: int
    barcode: str
    game: DhLotto645.Game
    result: str
    rank: int = None


class DhCoordinator(DataUpdateCoordinator):
    """동행복권 데이터 업데이트 코디네이터입니다."""

    client: DhLotteryClient


class DhLotteryCoordinator(DhCoordinator):
    """동행복권 데이터 업데이트 코디네이터입니다."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: DhLotteryClient,
        entry: Optional[ConfigEntry] = None,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="DhLotteryCoordinator",
            update_interval=COORDINATOR_UPDATE_INTERVAL,
        )
        self.client = client
        self.entry = entry
        self._balance_last_updated: Optional[datetime.datetime] = None
        self._accumulated_prize_last_updated: Optional[datetime.datetime] = None
        self._last_balance_alert_date: Optional[datetime.date] = None

    async def _async_update_data(self) -> dict[str, Any]:
        """동행복권 데이터를 비동기로 업데이트합니다."""
        now = datetime.datetime.now()

        # 새벽 0시 ~ 6시 (판매 중지 시간) 접속 차단 로직
        if 0 <= now.hour < 6 and self.data is not None:
            return self.data
        
        try:
            balance: Optional[DhLotteryBalanceData] = None
            if self._check_update_balance(now):
                async with async_timeout.timeout(10):
                    _LOGGER.info("예치금 정보를 업데이트합니다.")
                    try:
                        balance = await self.client.async_get_balance()
                        self._balance_last_updated = now
                    except Exception as balance_err:
                        _LOGGER.error(f"예치금 조회 실패 (통합구성요소는 계속 실행됩니다): {balance_err}")
                        # 이전 데이터가 있으면 유지, 없으면 기본값 사용
                        balance = self.data.get("balance") if self.data else None
            else:
                balance = self.data.get("balance") if self.data else None

            accumulated_prize: Optional[int] = None
            if self._check_update_accumulated_prize(now):
                async with async_timeout.timeout(10):
                    _LOGGER.info("누적 당첨금을 업데이트 합니다.")
                    try:
                        accumulated_prize = await self.client.async_get_accumulated_prize("LO40")
                        self._accumulated_prize_last_updated = now
                    except Exception as prize_err:
                        _LOGGER.warning(f"누적 당첨금 조회 실패 (무시됨): {prize_err}")
                        accumulated_prize = self.data.get("accumulated_prize") if self.data else None
            else:
                accumulated_prize = self.data.get("accumulated_prize") if self.data else None

            # 매주 목요일 오전 9시 예치금 사전 점검 및 알림
            await self._async_check_thursday_balance_alert(now, balance)

            return {
                "balance": balance,
                "accumulated_prize": accumulated_prize,
                "update_dt": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        except DhLotteryError as err:
            raise UpdateFailed(f"API와의 통신 오류: {err}")

    async def _async_check_thursday_balance_alert(
        self, now: datetime.datetime, balance: Optional[DhLotteryBalanceData]
    ) -> None:
        """매주 목요일 오전 9시에 이번 주 구매 필요 예치금을 사전 계산하고 알림을 보냅니다."""
        # 목요일(weekday == 3), 오전 9시(09:00 ~ 09:59)이고, 오늘 아직 알림을 보내지 않은 경우
        if now.weekday() != 3 or now.hour != 9 or self._last_balance_alert_date == now.date():
            return

        if balance is None:
            try:
                balance = await self.client.async_get_balance()
            except Exception as ex:
                _LOGGER.warning(f"목요일 예치금 점검 중 잔액 조회 실패: {ex}")
                return

        self._last_balance_alert_date = now.date()

        entry_options = self.entry.options if self.entry else {}
        entry_data = self.entry.data if self.entry else {}

        lotto_enabled = entry_options.get(CONF_LOTTO_645, entry_data.get(CONF_LOTTO_645, False))
        pension_enabled = entry_options.get(CONF_PENSION_720, entry_data.get(CONF_PENSION_720, False))

        lotto_cost = 0
        lotto_games_count = 0
        if lotto_enabled:
            for i in range(1, 6):
                game_str = entry_options.get(f"lotto_game_{i}", entry_data.get(f"lotto_game_{i}", "")).strip()
                if game_str:
                    lotto_games_count += 1
            if lotto_games_count == 0:
                lotto_games_count = 5
            lotto_cost = lotto_games_count * 1000

        pension_cost = 5000 if pension_enabled else 0
        total_cost = lotto_cost + pension_cost

        if total_cost == 0:
            return

        current_balance = balance.purchase_available
        is_shortage = current_balance < total_cost
        shortage_amt = max(0, total_cost - current_balance)

        details = []
        if lotto_enabled:
            details.append(f"로또 6/45 ({lotto_games_count}게임): {lotto_cost:,}원")
        if pension_enabled:
            details.append(f"연금복권 720+ (1세트): {pension_cost:,}원")
        details_str = ", ".join(details)

        if is_shortage:
            status_icon = "⚠️"
            status_title = "동행복권 예치금 부족 사전 안내"
            status_desc = f"- **상태**: ⚠️ **{shortage_amt:,}원 부족 (충전 필요)**\n💡 목요일 저녁 정기 자동 구매 전 동행복권 사이트에서 예치금을 충전해 주세요."
        else:
            status_icon = "📢"
            status_title = "동행복권 목요일 정기 예치금 점검"
            status_desc = "- **상태**: ✅ **정상 (구매 가능)**\n✅ 이번 주 복권 구매를 위한 예치금이 충분합니다."

        discord_msg = (
            f"{status_icon} **{status_title}**\n"
            f"- **점검일시**: {now.strftime('%Y-%m-%d %H:%M')}\n"
            f"- **현재 구매 가능 예치금**: {current_balance:,}원\n"
            f"- **이번 주 구매 필요 금액**: {total_cost:,}원 ({details_str})\n"
            f"{status_desc}"
        )

        webhook_url = entry_options.get("discord_webhook_url", entry_data.get("discord_webhook_url")) if self.entry else None
        if webhook_url:
            self.hass.async_create_task(self.client.async_send_to_discord(webhook_url, discord_msg))

        if is_shortage:
            persistent_notification.async_create(
                self.hass,
                f"현재 구매 가능 예치금({current_balance:,}원)이 이번 주 복권 구매 필요 금액({total_cost:,}원)보다 {shortage_amt:,}원 부족합니다. 원활한 자동 구매를 위해 충전해 주세요.",
                "동행복권 예치금 부족 알림",
                "donghaeng_thursday_balance_shortage"
            )

    async def async_clear_refresh(self):
        """데이터를 새로고침합니다."""
        self._balance_last_updated = None
        self._accumulated_prize_last_updated = None
        await self.async_request_refresh()

    def _check_update_balance(self, now: datetime.datetime) -> bool:
        """예치금 정보를 업데이트할지 확인합니다."""
        if not self._balance_last_updated:
            return True
        return (now - self._balance_last_updated) >= LOTTERY_BALANCE_UPDATE_INTERVAL

    def _check_update_accumulated_prize(self, now: datetime.datetime) -> bool:
        """누적 당첨금을 업데이트할지 확인합니다."""
        if not self._accumulated_prize_last_updated:
            return True
        return (
            now - self._accumulated_prize_last_updated
        ) >= LOTTERY_ACCUMULATED_PRIZE_UPDATE_INTERVAL


class DhLotto645Coordinator(DhCoordinator):
    """로또 6/45 데이터 업데이트 코디네이터입니다."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: DhLotteryClient,
        entry: Any,
        lottery_refresh_func: Callable[[], Awaitable[None]],
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="DhLotto645Coordinator",
            update_interval=COORDINATOR_UPDATE_INTERVAL,
        )
        self.client = client
        self.entry = entry
        self.lotto_645 = DhLotto645(client)
        self.lottery_refresh_func = lottery_refresh_func
        self._latest_winning_numbers: Optional[DhLotto645.WinningData] = None
        self._buy_history_last_updated: Optional[datetime.datetime] = None
        self.winning_dict: dict[int, DhLotto645.WinningData] = {}
        self._last_lotto_buy_attempt_time: Optional[datetime.datetime] = None
        self._last_lotto_buy_attempt_weekday: Optional[int] = None
        entry_id = entry.entry_id if entry else "default"
        self._notified_store = Store(hass, 1, f"donghaeng.{entry_id}_lotto_notified")
        self._notified_keys: Optional[list[str]] = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Lotto 6/45 데이터를 비동기로 업데이트합니다."""
        now = datetime.datetime.now()

        # 새벽 0시 ~ 6시 (판매 중지 시간) 접속 차단 로직
        if 0 <= now.hour < 6 and self.data is not None:
            return self.data
        
        try:
            latest_winning_numbers: Optional[DhLotto645.WinningData] = None

            if self._check_update_winning_numbers(now):
                async with async_timeout.timeout(10):
                    _LOGGER.info("당첨 번호를 업데이트합니다.")
                    latest_round_no = await self.lotto_645.async_get_latest_round_no()
                    latest_winning_numbers = await self._async_get_winning_numbers(
                        latest_round_no
                    )
                    self._latest_winning_numbers = latest_winning_numbers
                    # 최신 회차를 업데이트 할 때, 구매 내역, 예치금, 누적 당첨금이 같이 업데이트 되도록 함
                    if self._buy_history_last_updated:
                        await self.lottery_refresh_func()
                    self._buy_history_last_updated = None

            buy_history_this_week: List[DhLotto645BuyData] = []
            if self._async_check_update_buy_history(now):
                async with async_timeout.timeout(15):
                    _LOGGER.info("이번 주의 구매 내역을 업데이트합니다.")
                    buy_history_this_week = (
                        await self._async_get_buy_history_this_week()
                    )
                    self._buy_history_last_updated = now
                await self._async_process_result_notifications(buy_history_this_week)
            else:
                buy_history_this_week = self.data.get("buy_history_this_week", []) if self.data else []

            # 로또 6/45 백그라운드 자동 구매 스케줄러
            lotto_buy_time_str = self.entry.options.get("lotto_buy_time", self.entry.data.get("lotto_buy_time", "")) if self.entry else ""
            lotto_buy_weekday_str = self.entry.options.get("lotto_buy_weekday", self.entry.data.get("lotto_buy_weekday", "")) if self.entry else ""

            if lotto_buy_time_str and lotto_buy_weekday_str:
                weekday_map = {"월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3, "금요일": 4, "토요일": 5, "일요일": 6}
                configured_weekday = weekday_map.get(lotto_buy_weekday_str, -1)

                is_buy_time = False
                if now.weekday() == configured_weekday:
                    try:
                        buy_hour, buy_minute = map(int, lotto_buy_time_str.split(":"))
                        now_minutes = now.time().hour * 60 + now.time().minute
                        cfg_minutes = buy_hour * 60 + buy_minute
                        if 0 <= (now_minutes - cfg_minutes) < 10:
                            is_buy_time = True
                    except Exception as ex:
                        _LOGGER.error(f"로또 구매 시간 파싱 실패 ({lotto_buy_time_str}): {ex}")

                # 이미 이번 주에 구매한 내역이 있으면 건너뜨니다
                target_lotto_round = None
                if self._latest_winning_numbers:
                    target_lotto_round = self._latest_winning_numbers.round_no + 1
                already_bought = target_lotto_round is not None and any(
                    h.round_no == target_lotto_round for h in buy_history_this_week
                )

                # 쿨다운: 이번 요일에 이미 시도한 경우 건너뜨니다
                cooldown_active = (
                    self._last_lotto_buy_attempt_weekday == now.weekday()
                    and self._last_lotto_buy_attempt_time is not None
                    and (now - self._last_lotto_buy_attempt_time) < datetime.timedelta(hours=3)
                )

                if is_buy_time and not already_bought and not cooldown_active:
                    _LOGGER.info(f"설정된 로또 구매 시간 도달. 자동 구매를 시작합니다.")
                    self._last_lotto_buy_attempt_time = now
                    self._last_lotto_buy_attempt_weekday = now.weekday()
                    try:
                        from .client.dh_lotto_645 import DhLotto645SelMode
                        items = []
                        for i in range(1, 6):
                            game_str = self.entry.options.get(f"lotto_game_{i}", self.entry.data.get(f"lotto_game_{i}", "")).strip()
                            if not game_str:
                                continue
                            texts = [t.strip() for t in game_str.split(",")]
                            sel_mode = DhLotto645SelMode(texts[0])
                            if sel_mode == DhLotto645SelMode.AUTO:
                                items.append(DhLotto645.Slot(DhLotto645SelMode.AUTO))
                            else:
                                items.append(DhLotto645.Slot(sel_mode, [int(t) for t in texts[1:]]))

                        result = await self.lotto_645.async_buy(items)
                        self._buy_history_last_updated = None  # 캐시 무효화
                        await self.lottery_refresh_func()

                        number_text = "\n".join(
                            [f"- {game.slot} {game.mode} {' '.join(map(str, game.numbers))}" for game in result.games]
                        )
                        webhook_url = self.entry.options.get("discord_webhook_url", self.entry.data.get("discord_webhook_url")) if self.entry else None
                        if webhook_url:
                            remaining_balance = getattr(result, 'remaining_balance', 0)
                            discord_msg = (
                                f"📢 **[자동] 동행복권 로또 6/45 구매 완료**\n"
                                f"- **회차**: 제 {result.round_no}회\n"
                                f"- **구매일시**: {result.issue_dt}\n"
                                f"- **바코드**: {result.barcode}\n"
                                f"- **잔여 예치금**: {remaining_balance:,}원\n"
                                f"- **구매 번호**:\n{number_text}"
                            )
                            self.hass.async_create_task(self.client.async_send_to_discord(webhook_url, discord_msg))

                    except Exception as ex:
                        _LOGGER.error(f"백그라운드 로또 자동 구매 중 오류 발생: {ex}")
                        webhook_url = self.entry.options.get("discord_webhook_url", self.entry.data.get("discord_webhook_url")) if self.entry else None
                        if webhook_url:
                            discord_msg = f"❌ **[자동] 동행복권 로또 6/45 구매 실패**\n- **사유**: {str(ex)}"
                            self.hass.async_create_task(self.client.async_send_to_discord(webhook_url, discord_msg))

            return {
                "latest_winning_numbers": latest_winning_numbers,
                "buy_history_this_week": buy_history_this_week,
                "update_dt": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        # except DhLotteryLoginError as err:
        # Raising ConfigEntryAuthFailed will cancel future updates
        # and start a config flow with SOURCE_REAUTH (async_step_reauth)
        # raise ConfigEntryAuthFailed from err
        except DhLotteryError as err:
            raise UpdateFailed(f"API와의 통신 오류: {err}") from err

    async def async_clear_refresh(self):
        """데이터를 새로고침합니다."""
        self._latest_winning_numbers = None
        self._buy_history_last_updated = None
        self.winning_dict = {}
        await self.async_request_refresh()

    def _check_update_winning_numbers(self, now: datetime.datetime) -> bool:
        """당첨 번호를 업데이트할지 확인합니다."""
        if not self._latest_winning_numbers:
            return True
        # 현재 시각이 토요일 20:40 이후인지 확인합니다.
        if now.weekday() == 5 and now.time() >= datetime.time(20, 40):
            if now.strftime("%Y-%m-%d") != self._latest_winning_numbers.draw_date:
                return True
        # 토요일을 놓쳤더라도 최신 당첨 번호 날짜가 지난 주 토요일 이전이면 갱신
        elif now.weekday() != 5:
            days_since_saturday = (now.weekday() - 5) % 7
            last_saturday = (now - datetime.timedelta(days=days_since_saturday)).strftime("%Y-%m-%d")
            if self._latest_winning_numbers.draw_date < last_saturday:
                return True
        return False

    def _async_check_update_buy_history(self, now: datetime.datetime) -> bool:
        """구매 내역을 업데이트할지 확인합니다."""
        if not self._buy_history_last_updated:
            return True
        is_draw_window = (
            now.weekday() == 5
            and datetime.time(20, 40) <= now.time() <= datetime.time(23, 0)
        )
        has_pending = any(
            h.result == "미추첨" or h.rank == -1
            for h in (self.data.get("buy_history_this_week", []) if self.data else [])
        )
        interval = DRAW_RESULT_CHECK_INTERVAL if (is_draw_window and has_pending) else LOTTO_645_UPDATE_INTERVAL
        return (now - self._buy_history_last_updated) >= interval

    async def _async_get_buy_history_this_week(self) -> List[DhLotto645BuyData]:
        """이번 주의 구매 내역을 가져옵니다."""

        def calculate_rank(
            my_numbers: List[int], win_numbers: List[int], bonus: int
        ) -> int:
            """로또 등수를 계산합니다."""
            same_cnt = 0  # 일치하는 개수

            for num in win_numbers:  # 각 당첨 번호 포함 여부 체크
                if num in my_numbers:
                    same_cnt += 1
            # 등수 반환
            if same_cnt == 6:
                return 1
            if same_cnt == 5 and bonus in my_numbers:
                return 2
            if same_cnt == 5:
                return 3
            if same_cnt == 4:
                return 4
            if same_cnt == 3:
                return 5
            else:
                return 0  # 꽝

        async def async_get_rank(_result: str, _numbers: List[int]) -> int:
            """등수를 비동기로 가져옵니다."""
            if _result == "미추첨":
                return -1
            if "당첨" in _result:
                winning_numbers = await self._async_get_winning_numbers(item.round_no)
                return calculate_rank(
                    _numbers, winning_numbers.numbers, winning_numbers.bonus_num
                )
            return 0

        items: List[DhLotto645BuyData] = []
        for item in await self.lotto_645.async_get_buy_history_this_week():
            for game in item.games:
                items.append(
                    DhLotto645BuyData(
                        round_no=item.round_no,
                        barcode=item.barcode,
                        game=game,
                        rank=await async_get_rank(item.result, game.numbers),
                        result=item.result,
                    )
                )
        return items

    async def _async_get_winning_numbers(self, round_no: int):
        """당첨 번호를 비동기로 가져옵니다."""
        winning_data = self.winning_dict.get(round_no)
        if not winning_data:
            winning_data = await self.lotto_645.async_get_round_info(round_no)
            if (
                winning_data
                and winning_data.numbers
                and all(n is not None for n in winning_data.numbers)
            ):
                self.winning_dict[round_no] = winning_data
        return winning_data

    async def _async_process_result_notifications(
        self, buy_history: List[DhLotto645BuyData]
    ) -> None:
        """추첨이 완료된 로또 6/45 티켓 결과를 디스코드로 알립니다."""
        # 영구 저장소 로드 및 최초 도입 시 시딩 처리
        is_bootstrap = False
        store_updated = False
        if self._notified_keys is None:
            stored = await self._notified_store.async_load()
            if stored is None:
                is_bootstrap = True
                self._notified_keys = []
                store_updated = True
            else:
                self._notified_keys = list(stored.get("keys", []))

        if not buy_history:
            if store_updated:
                await self._notified_store.async_save({"keys": self._notified_keys})
            return

        # 티켓(회차, 바코드) 단위로 그룹화
        tickets: dict[tuple[int, str], list[DhLotto645BuyData]] = {}
        for item in buy_history:
            key_tuple = (item.round_no, item.barcode)
            tickets.setdefault(key_tuple, []).append(item)

        webhook_url = (
            self.entry.options.get("discord_webhook_url", self.entry.data.get("discord_webhook_url"))
            if self.entry
            else None
        )

        for (round_no, barcode), games_data in tickets.items():
            ticket_key = f"{round_no}_{barcode}"
            if ticket_key in self._notified_keys:
                continue

            # 아직 미추첨 상태이면 건너뜀
            if any(g.result == "미추첨" or g.rank == -1 for g in games_data):
                continue

            # API 반영 시차 방어: 당첨이라는데 당첨번호 미반영이거나 모두 0등으로 파싱된 경우 보류
            if any("당첨" in g.result for g in games_data):
                winning_data = self.winning_dict.get(round_no)
                if (
                    not winning_data
                    or int(winning_data.round_no) != int(round_no)
                    or not winning_data.numbers
                    or any(n is None for n in winning_data.numbers)
                ):
                    _LOGGER.debug(f"로또 {round_no}회 당첨번호 미반영으로 알림 보류")
                    continue
                if all(g.rank == 0 for g in games_data):
                    _LOGGER.debug(f"로또 {round_no}회 등수 반영 지연으로 알림 보류")
                    continue

            # 최초 도입(Bootstrap) 시에는 기존 완료 건을 알림 없이 기처리 시딩만 수행
            if is_bootstrap:
                self._notified_keys.append(ticket_key)
                store_updated = True
                continue

            if webhook_url:
                has_win = any(g.rank in (1, 2, 3, 4, 5) for g in games_data)
                header = (
                    f"🎉 **[로또 6/45] 제 {round_no}회 추첨 결과 (당첨!)**"
                    if has_win
                    else f"📋 **[로또 6/45] 제 {round_no}회 추첨 결과 (낙첨)**"
                )

                win_line = ""
                try:
                    winning_data = await self._async_get_winning_numbers(round_no)
                    if winning_data and int(winning_data.round_no) == int(round_no):
                        win_nums = ", ".join(map(str, winning_data.numbers))
                        win_line = f"- **당첨번호**: `{win_nums}` + 보너스 `{winning_data.bonus_num}`\n"
                except Exception:
                    pass

                game_lines = []
                for g in games_data:
                    nums_str = " ".join(map(str, g.game.numbers))
                    if g.rank in (1, 2, 3, 4, 5):
                        res_str = f"**{g.rank}등 당첨** 🎉"
                    else:
                        res_str = "낙첨"
                    game_lines.append(f"  - {g.game.slot} ({g.game.mode}) `{nums_str}` → {res_str}")

                discord_msg = (
                    f"{header}\n"
                    f"{win_line}"
                    f"- **바코드**: {barcode}\n"
                    f"- **구매 내역**:\n" + "\n".join(game_lines)
                )
                if len(discord_msg) > 1900:
                    discord_msg = discord_msg[:1897] + "..."
                self.hass.async_create_task(self.client.async_send_to_discord(webhook_url, discord_msg))

            self._notified_keys.append(ticket_key)
            store_updated = True

        if store_updated:
            self._notified_keys = self._notified_keys[-100:]
            await self._notified_store.async_save({"keys": self._notified_keys})


class DhPension720Coordinator(DhCoordinator):
    """연금복권 720+ 데이터 업데이트 코디네이터입니다."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: DhLotteryClient,
        entry: Any,
        lottery_refresh_func: Callable[[], Awaitable[None]],
    ):
        super().__init__(
            hass,
            _LOGGER,
            name="DhPension720Coordinator",
            update_interval=COORDINATOR_UPDATE_INTERVAL,
        )
        self.client = client
        self.entry = entry
        self.pension_720 = DhPension720(client)
        self.lottery_refresh_func = lottery_refresh_func
        self._latest_round_no: Optional[int] = None
        self._round_no_last_updated: Optional[datetime.datetime] = None
        self._buy_history_last_updated: Optional[datetime.datetime] = None
        self._last_pension_buy_attempt_time: Optional[datetime.datetime] = None
        self._last_pension_buy_attempt_round: Optional[int] = None
        entry_id = entry.entry_id if entry else "default"
        self._notified_store = Store(hass, 1, f"donghaeng.{entry_id}_pension_notified")
        self._notified_keys: Optional[list[str]] = None

    async def _async_update_data(self) -> dict[str, Any]:
        """연금복권 720+ 데이터를 비동기로 업데이트합니다."""
        now = datetime.datetime.now()

        # 새벽 0시 ~ 6시 (판매 중지 시간) 접속 차단 로직
        if 0 <= now.hour < 6 and self.data is not None:
            return self.data
        
        try:
            # 1. 최신 회차 번호 갱신
            if self._check_update_round_no(now) or self._latest_round_no is None:
                async with async_timeout.timeout(10):
                    _LOGGER.info("연금복권 최신 회차 번호를 업데이트합니다.")
                    latest_round_no = await self.pension_720.async_get_latest_round_no()
                    self._latest_round_no = latest_round_no
                    self._round_no_last_updated = now
                    if self._buy_history_last_updated:
                        await self.lottery_refresh_func()
                    self._buy_history_last_updated = None

            # 2. 이번 주 구매 내역 갱신
            buy_history_this_week: List[DhPension720BuyHistoryData] = []
            if self._async_check_update_buy_history(now) or self.data is None:
                async with async_timeout.timeout(15):
                    _LOGGER.info("이번 주의 연금복권 구매 내역을 업데이트합니다.")
                    buy_history_this_week = (
                        await self.pension_720.async_get_buy_history_this_week()
                    )
                    self._buy_history_last_updated = now
                await self._async_process_result_notifications(buy_history_this_week)
            else:
                buy_history_this_week = self.data.get("buy_history_this_week", [])

            # 3. 설정된 요일/시간에 따른 백그라운드 자동 구매 스케줄러 실행
            buy_time_str = self.entry.options.get("pension_buy_time", self.entry.data.get("pension_buy_time", "17:00"))
            buy_weekday_str = self.entry.options.get("pension_buy_weekday", self.entry.data.get("pension_buy_weekday", "목요일"))

            weekday_map = {"월요일": 0, "화요일": 1, "수요일": 2, "목요일": 3, "금요일": 4, "토요일": 5, "일요일": 6}
            configured_weekday = weekday_map.get(buy_weekday_str, 3)

            is_buy_time = False
            if now.weekday() == configured_weekday:
                try:
                    buy_hour, buy_minute = map(int, buy_time_str.split(":"))
                    # 설정 시각부터 정확히 5분 이내에만 구매 시도 (이후 재시도 방지)
                    configured_time = datetime.time(buy_hour, buy_minute)
                    now_time = now.time()
                    now_minutes = now_time.hour * 60 + now_time.minute
                    cfg_minutes = configured_time.hour * 60 + configured_time.minute
                    if 0 <= (now_minutes - cfg_minutes) < 10:
                        is_buy_time = True
                except Exception as ex:
                    _LOGGER.error(f"구매 시간 파싱 실패 (값: {buy_time_str}): {ex}")

            if is_buy_time and self._latest_round_no is not None:
                target_round = self._latest_round_no + 1
                already_bought = any(history.round_no == target_round for history in buy_history_this_week)
                
                # Cooldown check: Do not retry if we attempted in the last 3 hours for this target round
                cooldown_active = (
                    self._last_pension_buy_attempt_round == target_round
                    and self._last_pension_buy_attempt_time is not None
                    and (now - self._last_pension_buy_attempt_time) < datetime.timedelta(hours=3)
                )

                # 새벽 0시~6시 등 판매정지 시간이 아닐 때만 구매
                is_valid_sales_time = True
                if now.weekday() == 3 and 17 <= now.hour < 20: # 목요일 17~20시 판매 정지
                    is_valid_sales_time = False
                if 0 <= now.hour < 6: # 매일 0~6시 판매 정지
                    is_valid_sales_time = False

                if not already_bought and is_valid_sales_time and not cooldown_active:
                    _LOGGER.info(f"설정된 자동 구매 시간 도달. 연금복권 {target_round}회 자동 구매를 진행합니다.")
                    self._last_pension_buy_attempt_round = target_round
                    self._last_pension_buy_attempt_time = now
                    try:
                        raw_nums = self.entry.options.get("pension_manual_numbers", self.entry.data.get("pension_manual_numbers", "810212,810410,120911,150402"))
                        candidates = [x.strip() for x in raw_nums.split(",") if x.strip().isdigit() and len(x.strip()) == 6]
                        if not candidates:
                            candidates = ["810212", "810410", "120911", "150402"]

                        # 수동 입력 번호만 구매 시도. 모두 실패해도 자동 번호로 폴백하지 않습니다.
                        result = await self.pension_720.async_buy(candidates=candidates, allow_auto_fallback=False)
                        # 구매 후 내역 캐시를 즉시 무효화하여 already_bought가 다음 체크에서 정확히 반영되도록 합니다.
                        self._buy_history_last_updated = None
                        await self.lottery_refresh_func()
                        buy_history_this_week = await self.pension_720.async_get_buy_history_this_week()
                        
                        # Discord Webhook Notification
                        webhook_url = self.entry.options.get("discord_webhook_url", self.entry.data.get("discord_webhook_url")) if self.entry else None
                        if webhook_url:
                            number_text = "\n".join(
                                [
                                     f"- {game.group} {game.numbers} ({game.status})"
                                     for game in result.games
                                ]
                            )
                            failed_candidates_str = ", ".join(result.failed_candidates) if getattr(result, 'failed_candidates', None) else "없음"
                            success_candidate_str = result.success_candidate if getattr(result, 'success_candidate', None) else "없음"
                            sold_out_candidates_str = ", ".join(result.sold_out_candidates) if getattr(result, 'sold_out_candidates', None) else "없음"
                            remaining_balance = getattr(result, 'remaining_balance', 0)
                            
                            discord_msg = (
                                f"📢 **[자동] 동행복권 연금복권 720+ 구매 완료**\n"
                                f"- **회차**: 제 {result.round_no}회\n"
                                f"- **구매일시**: {result.issue_dt}\n"
                                f"- **주문번호**: {result.order_no}\n"
                                f"- **성공번호**: {success_candidate_str}\n"
                                f"- **실패번호**: {failed_candidates_str}\n"
                                f"- **매진으로 구매 안 함**: {sold_out_candidates_str}\n"
                                f"- **잔여 예치금**: {remaining_balance:,}원\n"
                                f"- **구매 번호**:\n{number_text}"
                            )
                            self.hass.async_create_task(self.client.async_send_to_discord(webhook_url, discord_msg))
                    except Exception as ex:
                        _LOGGER.error(f"백그라운드 연금복권 자동 구매 중 오류 발생: {ex}")
                        
                        # Discord Webhook Notification
                        webhook_url = self.entry.options.get("discord_webhook_url", self.entry.data.get("discord_webhook_url")) if self.entry else None
                        if webhook_url:
                            discord_msg = f"❌ **[자동] 동행복권 연금복권 720+ 구매 실패**\n- **사유**: {str(ex)}"
                            self.hass.async_create_task(self.client.async_send_to_discord(webhook_url, discord_msg))

            return {
                "latest_round_no": self._latest_round_no,
                "buy_history_this_week": buy_history_this_week,
                "update_dt": now.strftime("%Y-%m-%d %H:%M:%S"),
            }
        except DhLotteryError as err:
            raise UpdateFailed(f"API와의 통신 오류: {err}") from err

    async def async_clear_refresh(self):
        """데이터를 새로고침합니다."""
        self._latest_round_no = None
        self._round_no_last_updated = None
        self._buy_history_last_updated = None
        await self.async_request_refresh()

    def _check_update_round_no(self, now: datetime.datetime) -> bool:
        """최신 회차를 업데이트할지 확인합니다."""
        if not self._latest_round_no or not self._round_no_last_updated:
            return True
        # 목요일 19시 이후 추첨 완료 후에는 30분 간격으로 최신 회차 반영 확인
        if now.weekday() == 3 and now.hour >= 19:
            return (now - self._round_no_last_updated) >= datetime.timedelta(minutes=30)
        # 평시에는 12시간마다 최신 회차 점검
        return (now - self._round_no_last_updated) >= datetime.timedelta(hours=12)

    def _async_check_update_buy_history(self, now: datetime.datetime) -> bool:
        """구매 내역을 업데이트할지 확인합니다."""
        if not self._buy_history_last_updated:
            return True
        is_draw_window = (
            now.weekday() == 3
            and datetime.time(19, 15) <= now.time() <= datetime.time(21, 0)
        )
        has_pending = any(
            h.result == "미추첨" or any(g.rank == -1 for g in h.games)
            for h in (self.data.get("buy_history_this_week", []) if self.data else [])
        )
        interval = DRAW_RESULT_CHECK_INTERVAL if (is_draw_window and has_pending) else PENSION_720_UPDATE_INTERVAL
        return (now - self._buy_history_last_updated) >= interval

    async def _async_process_result_notifications(
        self, buy_history: List[DhPension720BuyHistoryData]
    ) -> None:
        """추첨이 완료된 연금복권 720+ 티켓 결과를 디스코드로 알립니다."""
        # 영구 저장소 로드 및 최초 도입 시 시딩 처리
        is_bootstrap = False
        store_updated = False
        if self._notified_keys is None:
            stored = await self._notified_store.async_load()
            if stored is None:
                is_bootstrap = True
                self._notified_keys = []
                store_updated = True
            else:
                self._notified_keys = list(stored.get("keys", []))

        if not buy_history:
            if store_updated:
                await self._notified_store.async_save({"keys": self._notified_keys})
            return

        webhook_url = (
            self.entry.options.get("discord_webhook_url", self.entry.data.get("discord_webhook_url"))
            if self.entry
            else None
        )

        for ticket in buy_history:
            ticket_key = f"{ticket.round_no}_{ticket.order_no}"
            if ticket_key in self._notified_keys:
                continue

            # 아직 미추첨 상태이거나 게임 목록이 비어있으면 건너뜀
            if (
                ticket.result == "미추첨"
                or not ticket.games
                or any(g.rank == -1 or g.status == "미추첨" for g in ticket.games)
            ):
                continue

            # API 반영 시차 방어: 당첨인데 모든 게임의 rank가 0으로 파싱되면 상세 정보 반영 지연으로 보고 보류
            if "당첨" in ticket.result and all(g.rank == 0 for g in ticket.games):
                _LOGGER.debug(f"연금복권 {ticket.round_no}회 상세 당첨 등수 반영 지연으로 알림 보류")
                continue

            # 최초 도입(Bootstrap) 시에는 기존 완료 건을 알림 없이 기처리 시딩만 수행
            if is_bootstrap:
                self._notified_keys.append(ticket_key)
                store_updated = True
                continue

            if webhook_url:
                has_win = any(g.rank > 0 for g in ticket.games)
                header = (
                    f"🎉 **[연금복권 720+] 제 {ticket.round_no}회 추첨 결과 (당첨!)**"
                    if has_win
                    else f"📋 **[연금복권 720+] 제 {ticket.round_no}회 추첨 결과 (낙첨)**"
                )

                game_lines = []
                for g in ticket.games:
                    if g.rank > 0:
                        res_str = f"**{g.status}** 🎉"
                    else:
                        res_str = g.status
                    game_lines.append(f"  - {g.group} `{g.numbers}` → {res_str}")

                discord_msg = (
                    f"{header}\n"
                    f"- **주문번호**: {ticket.order_no}\n"
                    f"- **구매 내역**:\n" + "\n".join(game_lines)
                )
                if len(discord_msg) > 1900:
                    discord_msg = discord_msg[:1897] + "..."
                self.hass.async_create_task(self.client.async_send_to_discord(webhook_url, discord_msg))

            self._notified_keys.append(ticket_key)
            store_updated = True

        if store_updated:
            self._notified_keys = self._notified_keys[-100:]
            await self._notified_store.async_save({"keys": self._notified_keys})

