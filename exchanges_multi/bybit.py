from passivbot_multi import Passivbot, logging
import ccxt.pro as ccxt_pro
import ccxt.async_support as ccxt_async
import pprint
import asyncio
import traceback
from pure_funcs import multi_replace, floatify, ts_to_date_utc, calc_hash, determine_pos_side_ccxt
from procedures import print_async_exception, utc_ms


class BybitBot(Passivbot):
    def __init__(self, config: dict):
        super().__init__(config)
        self.ccp = getattr(ccxt_pro, self.exchange)(
            {
                "apiKey": self.user_info["key"],
                "secret": self.user_info["secret"],
                "password": self.user_info["passphrase"],
            }
        )
        self.cca = getattr(ccxt_async, self.exchange)(
            {
                "apiKey": self.user_info["key"],
                "secret": self.user_info["secret"],
                "password": self.user_info["passphrase"],
            }
        )

    async def init_bot(self):
        # require symbols to be formatted to ccxt standard COIN/USDT:USDT
        self.markets = await self.cca.fetch_markets()
        self.markets_dict = {elm["symbol"]: elm for elm in self.markets}
        approved_symbols, self.approved_symbols_long, self.approved_symbols_short = [], [], []
        for symbol_ in sorted(set(self.config["symbols_long"] + self.config["symbols_short"])):
            symbol = symbol_
            if not symbol.endswith("/USDT:USDT"):
                coin_extracted = multi_replace(
                    symbol_, [("/", ""), (":", ""), ("USDT", ""), ("BUSD", ""), ("USDC", "")]
                )
                symbol_reformatted = coin_extracted + "/USDT:USDT"
                logging.info(
                    f"symbol {symbol_} is wrongly formatted. Trying to reformat to {symbol_reformatted}"
                )
                symbol = symbol_reformatted
            if symbol not in self.markets_dict:
                logging.info(f"{symbol} missing from {self.exchange}")
            else:
                elm = self.markets_dict[symbol]
                if elm["type"] != "swap":
                    logging.info(f"wrong market type for {symbol}: {elm['type']}")
                elif not elm["active"]:
                    logging.info(f"{symbol} not active")
                elif not elm["linear"]:
                    logging.info(f"{symbol} is not a linear market")
                else:
                    approved_symbols.append(symbol)
                    if symbol_ in self.config["live_configs_map"]:
                        self.config["live_configs_map"][symbol] = self.config["live_configs_map"][
                            symbol_
                        ]
                    if symbol_ in self.config["symbols_long"]:
                        self.approved_symbols_long.append(symbol)
                    if symbol_ in self.config["symbols_short"]:
                        self.approved_symbols_short.append(symbol)
        logging.info(f"approved symbols: {approved_symbols}")
        self.symbols = sorted(set(approved_symbols))
        self.quote = "USDT"
        self.inverse = False
        for symbol in approved_symbols:
            elm = self.markets_dict[symbol]
            self.symbol_ids[symbol] = elm["id"]
            self.min_costs[symbol] = (
                0.1 if elm["limits"]["cost"]["min"] is None else elm["limits"]["cost"]["min"]
            )
            self.min_qtys[symbol] = elm["limits"]["amount"]["min"]
            self.qty_steps[symbol] = elm["precision"]["amount"]
            self.price_steps[symbol] = elm["precision"]["price"]
            self.c_mults[symbol] = elm["contractSize"]
            self.coins[symbol] = symbol.replace("/USDT:USDT", "")
            self.tickers[symbol] = {"bid": 0.0, "ask": 0.0, "last": 0.0}
            self.open_orders[symbol] = []
            self.positions[symbol] = {
                "long": {"size": 0.0, "price": 0.0},
                "short": {"size": 0.0, "price": 0.0},
            }
            self.upd_timestamps["open_orders"][symbol] = 0.0
            self.upd_timestamps["tickers"][symbol] = 0.0
        await super().init_bot()

    async def start_webstockets(self):
        await asyncio.gather(
            self.watch_balance(),
            self.watch_orders(),
            self.watch_tickers(),
        )

    async def watch_balance(self):
        while True:
            try:
                if self.stop_websocket:
                    break
                res = await self.ccp.watch_balance()
                await self.handle_balance_update(res)
            except Exception as e:
                print(f"exception watch_balance", e)
                traceback.print_exc()

    async def watch_orders(self):
        while True:
            try:
                if self.stop_websocket:
                    break
                res = await self.ccp.watch_orders()
                await self.handle_order_update(res)
            except Exception as e:
                print(f"exception watch_orders", e)
                traceback.print_exc()

    async def watch_tickers(self, symbols=None):
        if symbols is None:
            symbols = self.symbols
        while True:
            try:
                if self.stop_websocket:
                    break
                res = await self.ccp.watch_tickers(symbols)
                await self.handle_ticker_update(res)
            except Exception as e:
                print(f"exception watch_tickers {symbols}", e)
                traceback.print_exc()

    async def fetch_open_orders(self, symbol: str = None):
        fetched = None
        open_orders = {}
        limit = 50
        try:
            fetched = await self.cca.fetch_open_orders(symbol=symbol, limit=limit)
            while True:
                if all([elm["id"] in open_orders for elm in fetched]):
                    break
                next_page_cursor = None
                for elm in fetched:
                    elm["position_side"] = determine_pos_side_ccxt(elm)
                    open_orders[elm["id"]] = elm
                    if "nextPageCursor" in elm["info"]:
                        next_page_cursor = elm["info"]["nextPageCursor"]
                if len(fetched) < limit:
                    break
                if next_page_cursor is None:
                    break
                # fetch more
                fetched = await self.cca.fetch_open_orders(
                    symbol=symbol, limit=limit, params={"cursor": next_page_cursor}
                )
            return sorted(open_orders.values(), key=lambda x: x["timestamp"])
        except Exception as e:
            logging.error(f"error fetching open orders {e}")
            print_async_exception(fetched)
            traceback.print_exc()
            return False

    async def fetch_positions(self):
        fetched = None
        positions = {}
        limit = 200
        try:
            fetched = await self.cca.fetch_positions(params={"limit": limit})
            while True:
                if all([elm["symbol"] + elm["side"] in positions for elm in fetched]):
                    break
                next_page_cursor = None
                for elm in fetched:
                    elm["position_side"] = determine_pos_side_ccxt(elm)
                    positions[elm["symbol"] + elm["side"]] = elm
                    if "nextPageCursor" in elm["info"]:
                        next_page_cursor = elm["info"]["nextPageCursor"]
                    positions[elm["symbol"] + elm["side"]] = elm
                if len(fetched) < limit:
                    break
                if next_page_cursor is None:
                    break
                # fetch more
                fetched = await self.cca.fetch_positions(
                    params={"cursor": next_page_cursor, "limit": limit}
                )
            return sorted(positions.values(), key=lambda x: x["timestamp"])
        except Exception as e:
            logging.error(f"error fetching open orders {e}")
            print_async_exception(fetched)
            traceback.print_exc()
            return False

    async def fetch_balance(self):
        fetched = None
        try:
            fetched = await self.cca.fetch_balance()
            return fetched[self.quote]["total"]
        except Exception as e:
            logging.error(f"error fetching balance {e}")
            print_async_exception(fetched)
            traceback.print_exc()
            return False

    async def fetch_tickers(self):
        fetched = None
        try:
            fetched = await self.cca.fetch_tickers()
            return fetched
        except Exception as e:
            logging.error(f"error fetching tickers {e}")
            print_async_exception(fetched)
            traceback.print_exc()
            return False

    async def fetch_ohlcv(self, symbol: str, timeframe="1m"):
        # intervals: 1,3,5,15,30,60,120,240,360,720,D,M,W
        fetched = None
        try:
            fetched = await self.cca.fetch_ohlcv(symbol, timeframe=timeframe, limit=1000)
            return fetched
        except Exception as e:
            logging.error(f"error fetching ohlcv for {symbol} {e}")
            print_async_exception(fetched)
            traceback.print_exc()
            return False

    async def fetch_pnls(
        self,
        symbol: str = None,
        start_time: int = None,
        end_time: int = None,
    ):
        if start_time is not None:
            week = 1000 * 60 * 60 * 24 * 7
            income = []
            if end_time is None:
                end_time = int(utc_ms() + 1000 * 60 * 60 * 24)
            # bybit has limit of 7 days per pageinated fetch
            # fetch multiple times
            i = 1
            while i < 52:  # limit n fetches to 52 (one year)
                sts = end_time - week * i
                ets = sts + week
                sts = max(sts, start_time)
                fetched = await self.fetch_pnl(symbol=symbol, start_time=sts, end_time=ets)
                income.extend(fetched)
                if sts <= start_time:
                    break
                i += 1
                logging.debug(f"fetching income for more than a week {ts_to_date_utc(sts)}")
                print(f"fetching income for more than a week {ts_to_date_utc(sts)}")
            return sorted(
                {elm["orderId"]: elm for elm in income}.values(), key=lambda x: x["updatedTime"]
            )
        else:
            return await self.fetch_pnl(symbol=symbol, start_time=start_time, end_time=end_time)

    async def fetch_pnl(
        self,
        symbol: str = None,
        start_time: int = None,
        end_time: int = None,
    ):
        fetched = None
        income_d = {}
        limit = 100
        try:
            params = {"category": "linear", "limit": limit}
            if symbol is not None:
                params["symbol"] = symbol
            if start_time is not None:
                params["startTime"] = int(start_time)
            if end_time is not None:
                params["endTime"] = int(end_time)
            fetched = await self.cca.private_get_v5_position_closed_pnl(params)
            fetched["result"]["list"] = sorted(
                floatify(fetched["result"]["list"]), key=lambda x: x["updatedTime"]
            )
            while True:
                if fetched["result"]["list"] == []:
                    break
                print(
                    f"fetching income {ts_to_date_utc(fetched['result']['list'][-1]['updatedTime'])}"
                )
                logging.debug(
                    f"fetching income {ts_to_date_utc(fetched['result']['list'][-1]['updatedTime'])}"
                )
                if (
                    fetched["result"]["list"][0]["orderId"] in income_d
                    and fetched["result"]["list"][-1]["orderId"] in income_d
                ):
                    break
                for elm in fetched["result"]["list"]:
                    income_d[elm["orderId"]] = elm
                if start_time is None:
                    break
                if fetched["result"]["list"][0]["updatedTime"] <= start_time:
                    break
                if not fetched["result"]["nextPageCursor"]:
                    break
                params["cursor"] = fetched["result"]["nextPageCursor"]
                fetched = await self.cca.private_get_v5_position_closed_pnl(params)
                fetched["result"]["list"] = sorted(
                    floatify(fetched["result"]["list"]), key=lambda x: x["updatedTime"]
                )
            return sorted(income_d.values(), key=lambda x: x["updatedTime"])
        except Exception as e:
            logging.error(f"error fetching income {e}")
            print_async_exception(fetched)
            traceback.print_exc()
            return []