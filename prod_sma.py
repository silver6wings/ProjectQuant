"""
S = 20
M = 40
N = 60
CLOSE > OPEN
(CLOSE - OPEN) > 0.0618 * CLOSE
CLOSE > MA(CLOSE, N)
OPEN < MA(CLOSE, N)
CLOSE > MA(CLOSE, M)
OPEN < MA(CLOSE, M)
CLOSE > MA(CLOSE, S)
"""
import logging
import datetime
import threading

from typing import List, Dict

from xtquant import xtdata
from data_loader.reader_xtdata import get_xtdata_market_dict
from tools.utils_basic import logging_init, symbol_to_code
from tools.utils_cache import get_all_historical_symbols, daily_once, check_today_is_open_day
from tools.utils_xtdata import get_prev_trading_date
from tools.xt_subscriber import sub_whole_quote
# from tools.xt_delegate import XtDelegate

# ======== 策略常量 ========

strategy_name = '银狐一号'

my_client_path = r'C:\国金QMT交易端模拟\userdata_mini'
my_account_id = '55009728'
# my_account_id = '55010470'

target_stock_prefix = [
    '000', '001', '002', '003',
    '300', '301',
    '600', '601', '603', '605',
]

path_held = '_cache/prod_sma/held_days.json'  # 记录持仓日期
path_date = '_cache/prod_sma/curr_date.json'  # 用来标记每天执行一次任务的缓存
path_logs = '_cache/prod_sma/log.txt'  # 用来存储选股和委托操作

# ======== 全局变量 ========

my_quotes_update_lock = threading.Lock()    # 更新quotes缓存用的锁
my_daily_prepare_lock = threading.Lock()    # 每天更新历史数据用的锁

quotes_cache: Dict[str, Dict] = {}                  # 记录实时价格信息
select_cache: Dict[str, List] = {}                  # 记录选股历史，目的是为了去重
indicators_cache: Dict[str, Dict[str, float]] = {}  # 记录历史指标信息

time_cache = {
    'prev_datetime': '',    # 限制每秒执行一次的缓存
    'prev_minutes': '',     # 限制每分钟屏幕打印心跳的缓存
}


class p:
    S = 20
    M = 40
    N = 60
    open_inc = 0.02


def prepare_indicator_source() -> dict:
    history_symbols = get_all_historical_symbols()
    history_codes = [
        symbol_to_code(symbol)
        for symbol in history_symbols
        if symbol[:3] in target_stock_prefix
    ]

    now = datetime.datetime.now()
    start = get_prev_trading_date(now, 59)
    end = get_prev_trading_date(now, 1)
    print(f'Fetching qmt history data from {start} to {end}')

    t0 = datetime.datetime.now()
    group_size = 500
    count = 0

    for i in range(0, len(history_codes), group_size):
        sub_codes = [sub_code for sub_code in history_codes[i:i + group_size]]
        print(f'Preparing {sub_codes}')
        data = get_xtdata_market_dict(
            codes=sub_codes,
            start_date=start,
            end_date=end,
            columns=['close'],
        )

        for code in sub_codes:
            row = data['close'].loc[code]
            if not row.isna().any() and len(row) == 59:
                count += 1
                indicators_cache[code] = {
                    'ma19': row.tail(19).mean(),
                    'ma39': row.tail(39).mean(),
                    'ma59': row.tail(59).mean(),
                }

    t1 = datetime.datetime.now()
    print(f'Preparing time cost: {t1 - t0}')
    print(f'{count} stocks prepared.')

    return indicators_cache


def stock_selection(quote: dict, indicator: dict) -> (bool, dict):
    p_close = quote['lastPrice']
    p_open = quote['open']

    if not p_close > p_open:
        return False, {}

    if not p_close > p_open * p.open_inc:
        return False, {}

    ma_60 = (indicator['ma59'] * 59.0 + p_close) / 60.0
    if not (p_open < ma_60 < p_close):
        return False, {}

    ma_40 = (indicator['ma39'] * 39.0 + p_close) / 40.0
    if not (p_open < ma_40 < p_close):
        return False, {}

    ma_20 = (indicator['ma19'] * 19.0 + p_close) / 20.0
    if not (p_open < ma_20 < p_close):
        return False, {}

    return True, {'sma20': ma_20, 'sma40': ma_40, 'sma60': ma_60}


def scan_buy(quotes: dict, curr_date: str):
    selections = []
    for code in quotes:
        if code[:3] not in target_stock_prefix:
            continue

        if code not in indicators_cache:
            continue

        passed, info = stock_selection(quotes[code], indicators_cache[code])
        if passed:
            selection = {'code': code, 'price': quotes[code]["lastPrice"]}
            selection.update(info)
            selections.append(selection)

    if len(selections) > 0:  # 选出一个以上的股票
        # 记录选股历史
        if curr_date not in select_cache.keys():
            select_cache[curr_date] = []

        for selection in selections:
            if selection['code'] not in select_cache[curr_date]:
                select_cache[curr_date].append(selection['code'])
                logging.warning('选股 {}\t现价: {}\tSMA 20: {}\tSMA 40: {}\tSMA 60: {}'.format(
                    selection["code"],
                    round(selection["price"], 2),
                    round(selection["sma20"], 2),
                    round(selection["sma40"], 2),
                    round(selection["sma60"], 2),
                ))


def execute_strategy(curr_date, curr_time, quotes):
    # 预备
    if '09:10' <= curr_time <= '09:14':
        daily_once(
            my_daily_prepare_lock, time_cache, path_date, '_daily_once_prepare_ind',
            curr_date, prepare_indicator_source)

    # 早盘
    elif '09:30' <= curr_time <= '11:30':
        scan_buy(quotes, curr_date)

    # 午盘
    elif '13:00' <= curr_time <= '14:56':
        scan_buy(quotes, curr_date)


def callback_sub_whole(quotes: dict) -> None:
    now = datetime.datetime.now()

    # 每分钟输出一行开头
    curr_time = now.strftime('%H:%M')
    if time_cache['prev_minutes'] != curr_time:
        time_cache['prev_minutes'] = curr_time
        print(f'\n[{curr_time}]', end='')

    # 每秒钟开始的时候输出一个点
    with my_quotes_update_lock:
        curr_datetime = now.strftime("%Y-%m-%d %H:%M:%S")
        quotes_cache.update(quotes)
        if time_cache['prev_datetime'] != curr_datetime:
            time_cache['prev_datetime'] = curr_datetime
            curr_date = now.strftime('%Y-%m-%d')

            # 只有在交易日才执行策略
            if check_today_is_open_day(curr_date):
                print('.', end='')
                execute_strategy(curr_date, curr_time, quotes_cache)
                quotes_cache.clear()
            else:
                print('x', end='')


if __name__ == '__main__':
    logging_init(path=path_logs, level=logging.INFO)

    # xt_delegate = XtDelegate(
    #     account_id=my_account_id,
    #     client_path=my_client_path,
    #     xt_callback=None,
    # )

    today = datetime.datetime.now().strftime('%Y-%m-%d')
    daily_once(
        my_daily_prepare_lock, time_cache, None, '_daily_once_prepare_ind',
        today, prepare_indicator_source)

    print('启动行情订阅...')
    check_today_is_open_day(datetime.datetime.now().strftime('%Y-%m-%d'))
    sub_whole_quote(callback_sub_whole)
    xtdata.run()  # 死循环 阻塞主线程退出
