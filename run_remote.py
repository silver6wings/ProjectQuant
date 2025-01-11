import time
import math
import logging
import schedule

from credentials import *

from tools.utils_basic import logging_init, is_symbol
from tools.utils_cache import *
from tools.utils_ding import DingMessager

from delegate.xt_delegate import xt_get_ticks
from delegate.xt_subscriber import XtSubscriber, update_position_held

from trader.buyer import BaseBuyer as Buyer
from trader.pools import StocksPoolBlackEmpty as Pool
from trader.seller_groups import LTT2GroupSeller as Seller

from tools.utils_remote import pull_stock_codes

# ======== 配置 ========

STRATEGY_NAME = '远程选股'
SELECTION_ID = 'LTDB'
DING_MESSAGER = DingMessager(DING_SECRET, DING_TOKENS)
IS_PROD = True
IS_DEBUG = True

PATH_BASE = CACHE_BASE_PATH

PATH_ASSETS = PATH_BASE + '/assets.csv'         # 记录历史净值
PATH_DEAL = PATH_BASE + '/deal_hist.csv'        # 记录历史成交
PATH_HELD = PATH_BASE + '/held_days.json'       # 记录持仓日期
PATH_MAXP = PATH_BASE + '/max_price.json'       # 记录历史最高
PATH_LOGS = PATH_BASE + '/logs.txt'             # 用来存储选股和委托操作
PATH_INFO = PATH_BASE + '/temp_{}.pkl'          # 用来缓存当天的指标信息

lock_of_disk_cache = threading.Lock()           # 操作磁盘文件缓存的锁

cache_selected: Dict[str, Set] = {}             # 记录选股历史，去重


def debug(*args):
    if IS_DEBUG:
        print(*args)


class PoolConf:
    day_count = 100         # 100个自然日里有 > 60个交易日
    price_adjust = 'qfq'    # 历史价格复权
    columns = ['datetime', 'open', 'high', 'low', 'close', 'volume', 'amount']


class BuyConf:
    time_ranges = [['09:31', '11:00']]

    interval = 15
    order_premium = 0.09    # 保证成功买入成交的溢价

    slot_count = 20         # 持股数量上限
    slot_capacity = 30000   # 每个仓的资金上限
    once_buy_limit = 20     # 单次选股最多买入股票数量（若单次未买进当日不会再买这只

    min_price = 2.00        # 限制最低可买入股票的现价
    inc_limit = 1.20        # 相对于昨日收盘的涨幅限制


class SellConf:
    time_ranges = [['09:30', '11:30'], ['13:00', '15:00']]

    interval = 5
    order_premium = 0.09            # 保证成功卖出成交的溢价

    switch_hold_days = 5            # 持仓天数
    switch_demand_daily_up = 0.003  # 换仓上限乘数
    switch_begin_time = '14:30'     # 每天最早换仓时间

    earn_limit = 9.999              # 硬性止盈率
    risk_limit = 1 - 0.07           # 硬性止损率
    risk_tight = 0.002              # 硬性止损率每日上移

    return_of_profit = [            # 至少保留收益范围
        (1.16, 9.99, 0.16),
        (1.07, 1.16, 0.33),
        (1.04, 1.07, 0.55),
        (1.02, 1.04, 0.66),
    ]

    cci_upper = 370.0               # cci 高卖点阈值
    cci_lower = -70.0                # cci 低卖点阈值

    open_low_rate = 0.99            # 低于开仓日最低价比例
    open_vol_rate = 0.30            # 低于开仓日成交量比例
    tail_vol_time = '14:40'         # 低于开仓日成交量执行时间

    ma_above = 30                   # 跌破N日均线卖出


# ======== 盘前 ========


def held_increase() -> None:
    if not check_today_is_open_day(datetime.datetime.now().strftime('%Y-%m-%d')):
        return

    update_position_held(lock_of_disk_cache, my_delegate, PATH_HELD)
    if all_held_inc(lock_of_disk_cache, PATH_HELD):
        logging.warning('===== 所有持仓计数 +1 =====')
        print(f'All held stock day +1!')


def refresh_code_list():
    if not check_today_is_open_day(datetime.datetime.now().strftime('%Y-%m-%d')):
        return

    my_pool.refresh()
    positions = my_delegate.check_positions()
    hold_list = [position.stock_code for position in positions if is_symbol(position.stock_code)]
    my_suber.update_code_list(my_pool.get_code_list() + hold_list)


def prepare_history() -> None:
    if not check_today_is_open_day(datetime.datetime.now().strftime('%Y-%m-%d')):
        return

    now = datetime.datetime.now()
    curr_date = now.strftime('%Y-%m-%d')
    cache_path = PATH_INFO.format(curr_date)

    start = get_prev_trading_date(now, PoolConf.day_count)
    end = get_prev_trading_date(now, 1)

    # 只有持仓列表
    positions = my_delegate.check_positions()
    holding_list = [position.stock_code for position in positions if is_symbol(position.stock_code)]

    my_suber.download_cache_history(
        cache_path=cache_path,
        code_list=holding_list,
        start=start,
        end=end,
        adjust=PoolConf.price_adjust,
        columns=PoolConf.columns,
    )


# ======== 买点 ========


def check_stock_codes(selected_codes: list[str], quotes: Dict,) -> List[Dict[str, any]]:
    selections = []

    for code in selected_codes:
        if code not in quotes:
            debug(code, f'本次quotes没数据')
            continue

        # if code not in my_pool.cache_whitelist:
        #     debug(code, f'不在白名单')
        #     continue

        # if code in my_pool.cache_blacklist:
        #     debug(code, f'在黑名单')
        #     continue

        quote = quotes[code]

        # prev_close = quote['lastClose']
        # curr_open = quote['open']
        curr_price = quote['lastPrice']

        if not curr_price > BuyConf.min_price:
            debug(code, f'价格小于{BuyConf.min_price}')
            continue

        # if not curr_open < curr_price < prev_close * BuyConf.inc_limit:
        #     debug(code, f'涨幅不符合区间 {curr_open} < {curr_price} < {prev_close * BuyConf.inc_limit}')
        #     continue

        selection = {
            'code': code,
            'price': round(max(quote['askPrice'] + [curr_price]), 2),
            'lastClose': round(quote['lastClose'], 2),
        }
        selections.append(selection)

    return selections


def scan_buy(quotes: Dict, curr_date: str, positions: List) -> None:
    selected_codes, err_msg = pull_stock_codes(SELECTION_ID, RECOMMEND_HOST, AUTHENTICATION)
    if selected_codes is None:
        print(f'[{err_msg}]', end='')
        return
    else:
        print(f'[{len(selected_codes)}]')

    selections = []
    if len(selected_codes) > 0:
        quotes = xt_get_ticks(selected_codes)
        selections = check_stock_codes(selected_codes, quotes) if selected_codes is not None else []
        debug(len(quotes), selected_codes, selections)
        debug(quotes)

    if len(selections) > 0:
        position_codes = [position.stock_code for position in positions]
        position_count = get_holding_position_count(positions)
        available_cash = my_delegate.check_asset().cash
        available_slot = available_cash // BuyConf.slot_capacity

        buy_count = max(0, BuyConf.slot_count - position_count)     # 确认剩余的仓位
        buy_count = min(buy_count, available_slot)                  # 确认现金够用
        buy_count = min(buy_count, len(selections))                 # 确认选出的股票够用
        buy_count = min(buy_count, BuyConf.once_buy_limit)          # 限制一秒内下单数量
        buy_count = int(buy_count)

        for i in range(len(selections)):  # 依次买入
            # logging.info(f'买数相关：持仓{position_count} 现金{available_cash} 已选{len(selections)}')
            if buy_count > 0:
                code = selections[i]['code']
                price = selections[i]['price']
                last_close = selections[i]['lastClose']
                buy_volume = math.floor(BuyConf.slot_capacity / price / 100) * 100

                if buy_volume <= 0:
                    debug(f'{code} 不够一手')
                elif code in position_codes:
                    debug(f'{code} 正在持仓')
                elif curr_date in cache_selected and code in cache_selected[curr_date]:
                    debug(f'{code} 今日已选')
                else:
                    buy_count = buy_count - 1
                    # 如果今天未被选股过 and 目前没有持仓则记录（意味着不会加仓
                    my_buyer.order_buy(code=code, price=price, last_close=last_close,
                                       volume=buy_volume, remark='市价买入')
            else:
                break

    # 记录选股历史
    if curr_date not in cache_selected:
        cache_selected[curr_date] = set()

    for selection in selections:
        if selection['code'] not in cache_selected[curr_date]:
            cache_selected[curr_date].add(selection['code'])
            logging.warning(f"记录选股 {selection['code']}\t现价: {selection['price']:.2f}")


# ======== 卖点 ========


def scan_sell(quotes: Dict, curr_date: str, curr_time: str, positions: List) -> None:
    max_prices, held_days = update_max_prices(lock_of_disk_cache, quotes, positions, PATH_MAXP, PATH_HELD)
    my_seller.execute_sell(quotes, curr_date, curr_time, positions, held_days, max_prices, my_suber.cache_history)


# ======== 框架 ========


def execute_strategy(curr_date: str, curr_time: str, curr_seconds: str, curr_quotes: Dict) -> bool:
    positions = my_delegate.check_positions()

    for time_range in SellConf.time_ranges:
        if time_range[0] <= curr_time <= time_range[1]:
            if int(curr_seconds) % SellConf.interval == 0:
                scan_sell(curr_quotes, curr_date, curr_time, positions)

    for time_range in BuyConf.time_ranges:
        if time_range[0] <= curr_time <= time_range[1]:
            if int(curr_seconds) % BuyConf.interval == 0:
                scan_buy(curr_quotes, curr_date, positions)
                return True

    return True


if __name__ == '__main__':
    logging_init(path=PATH_LOGS, level=logging.INFO)
    print(f'正在启动 {STRATEGY_NAME}{"" if IS_PROD else "(模拟)"}...')
    if IS_PROD:
        from delegate.xt_callback import XtCustomCallback
        from delegate.xt_delegate import XtDelegate, get_holding_position_count

        my_callback = XtCustomCallback(
            account_id=QMT_ACCOUNT_ID,
            strategy_name=STRATEGY_NAME,
            ding_messager=DING_MESSAGER,
            lock_of_disk_cache=lock_of_disk_cache,
            path_deal=PATH_DEAL,
            path_held=PATH_HELD,
            path_maxp=PATH_MAXP,
        )
        my_delegate = XtDelegate(
            account_id=QMT_ACCOUNT_ID,
            client_path=QMT_CLIENT_PATH,
            callback=my_callback,
        )
    else:
        from delegate.gm_callback import GmCallback
        from delegate.gm_delegate import GmDelegate, get_holding_position_count

        my_callback = GmCallback(
            account_id=QMT_ACCOUNT_ID,
            strategy_name=STRATEGY_NAME,
            ding_messager=DING_MESSAGER,
            lock_of_disk_cache=lock_of_disk_cache,
            path_deal=PATH_DEAL,
            path_held=PATH_HELD,
            path_maxp=PATH_MAXP,
        )
        my_delegate = GmDelegate(
            account_id=QMT_ACCOUNT_ID,
            callback=my_callback,
            ding_messager=DING_MESSAGER,
        )

    my_pool = Pool(
        account_id=QMT_ACCOUNT_ID,
        strategy_name=STRATEGY_NAME,
        parameters=PoolConf,
        ding_messager=DING_MESSAGER,
    )
    my_buyer = Buyer(
        account_id=QMT_ACCOUNT_ID,
        strategy_name=STRATEGY_NAME,
        delegate=my_delegate,
        parameters=BuyConf,
    )
    my_seller = Seller(
        strategy_name=STRATEGY_NAME,
        delegate=my_delegate,
        parameters=SellConf,
    )
    my_suber = XtSubscriber(
        account_id=QMT_ACCOUNT_ID,
        strategy_name=STRATEGY_NAME,
        delegate=my_delegate,
        path_deal=PATH_DEAL,
        path_assets=PATH_ASSETS,
        execute_strategy=execute_strategy,
        ding_messager=DING_MESSAGER,
    )
    my_suber.start_scheduler()

    temp_now = datetime.datetime.now()
    temp_date = temp_now.strftime('%Y-%m-%d')
    temp_time = temp_now.strftime('%H:%M')

    # 定时任务启动
    schedule.every().day.at('09:10').do(held_increase)
    schedule.every().day.at('09:11').do(refresh_code_list)
    schedule.every().day.at('09:15').do(prepare_history)    # 必须先 refresh code list

    if '09:10' < temp_time < '15:30' and check_today_is_open_day(temp_date):
        held_increase()
        refresh_code_list()
        prepare_history()  # 重启时防止没有数据在这先加载历史数据

        if '09:30' <= temp_time <= '11:30' or '13:00' <= temp_time <= '14:57':
            my_suber.subscribe_tick()  # 重启时如果在交易时间则订阅Tick

    try:
        while True:
            schedule.run_pending()
            time.sleep(1)
    finally:
        schedule.clear()
        my_delegate.shutdown()
