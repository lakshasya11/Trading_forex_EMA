import MetaTrader5 as mt5
import time
from datetime import datetime
import pytz
import os
from dotenv import load_dotenv
from enhanced_strategy import EnhancedTradingStrategy
# Removed unused imports - all logic is now inline

# ANSI color codes for terminal highlighting
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    MAGENTA = '\033[95m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    RESET = '\033[0m'

def update_mt5_stop_loss(ticket, new_sl):
    """Update stop loss for existing position"""
    try:
        # Get current position info
        pos = mt5.positions_get(ticket=ticket)
        if pos:
            current_sl = pos[0].sl
            print(f"[SL_DEBUG] Current MT5 SL: {current_sl:.5f} | Calculated SL: {new_sl:.5f}")
        
        request = {
            'action': mt5.TRADE_ACTION_SLTP,
            'position': ticket,
            'sl': round(new_sl, 5),
            'magic': 123456
        }
        result = mt5.order_send(request)
        success = result and result.retcode == mt5.TRADE_RETCODE_DONE
        
        if not success:
            print(f"[SL_FAILED] Error: {result.comment if result else 'Unknown'}")
        else:
            print(f"[SL_SUCCESS] Updated to {new_sl:.5f}")
            
        return success
    except Exception as e:
        print(f"[ERROR] SL Update failed: {e}")
        return False
    
def calculate_trailing_stop_2dollar(pos_type, entry_price, current_price, pos_ticket, logger):
    """Trailing SL: Activates after $10 profit, maintains $2 distance from current price"""
    price_movement = (entry_price - current_price) if pos_type == "BUY" else (current_price - entry_price)
    
    if pos_ticket not in logger.trailing_stop_2dollar:
        logger.trailing_stop_2dollar[pos_ticket] = None
    
    current_sl = logger.trailing_stop_2dollar[pos_ticket]
    
    if price_movement >= 10.0:
        if pos_type == "BUY":
            new_sl = current_price - 2.0
            if current_sl is None or new_sl > current_sl:
                logger.trailing_stop_2dollar[pos_ticket] = new_sl
                return True, new_sl
        else:
            new_sl = current_price + 2.0
            if current_sl is None or new_sl < current_sl:
                logger.trailing_stop_2dollar[pos_ticket] = new_sl
                return True, new_sl
    
    return False, current_sl



class SignalConfirmation:
    def __init__(self, required_confirmations=3):
        self.required_confirmations = 3
        self.base_confirmations = 3
        self.signal_history = []
        self.direction_changes = []
        self.reset_required = False  # NEW: Require clean slate after reset
        self.seen_none_after_reset = False  # NEW: Track if we've seen NONE signal
    
    def detect_flickering(self):
        return False
    
    def add_signal(self, signal, supertrend_direction):
               
        self.signal_history.append(signal)
        self.direction_changes.append(supertrend_direction)
        
        if len(self.signal_history) > 10:
            self.signal_history.pop(0)
            self.direction_changes.pop(0)
            self.required_confirmations = 3
        
        if len(self.signal_history) < 3:
            return None, 0, 3
        
        buy_count = self.signal_history[-3:].count("BUY")
        sell_count = self.signal_history[-3:].count("SELL")
        
        if buy_count == 3:
            return "BUY", buy_count, 3
        elif sell_count == 3:
            return "SELL", sell_count, 3
        return None, max(buy_count, sell_count), 3
    
    def reset(self):
        self.signal_history = []
        self.direction_changes = []
        self.required_confirmations = 3
    
# ADD THESE LINES HERE (Lines 48-72):
def fetch_candle_history(symbol, strategy, num_candles=10):
    """Fetch last N candles with OHLC and SuperTrend values"""
    import pandas as pd
    
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, num_candles + 50)
    if rates is None or len(rates) < num_candles:
        return []
    
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    
    st_data = strategy.calculate_supertrend_pinescript(df, atr_length=10, atr_multiplier=0.9, smoothing_period=1)

    
    candle_data = []
    for i in range(-num_candles, 0):
        candle_data.append({
            'time': df['time'].iloc[i],
            'open': df['open'].iloc[i],
            'high': df['high'].iloc[i],
            'low': df['low'].iloc[i],
            'close': df['close'].iloc[i],
            'supertrend': st_data['supertrend'].iloc[i],
            'direction': st_data['direction'].iloc[i]
        })
    
    return candle_data

def display_supertrend_stoploss(strategy, symbol, current_price):
    """Display SuperTrend Stop Loss values prominently"""
    analysis = strategy.analyze_timeframe('M5')
    if not analysis:
        return
    
    direction = analysis.get('supertrend_direction', 0)
    st_value = analysis.get('supertrend_value', 0)
    st_exit = analysis.get('supertrend_exit_value', 0)
    trend_sl = analysis.get('trend_extreme_sl', 0)  
    
    dir_color = Colors.GREEN if direction == 1 else Colors.RED
    dir_text = "BULLISH" if direction == 1 else "BEARISH"
    
    print(f"\n{Colors.CYAN}{'═'*60}{Colors.RESET}")
    print(f"{Colors.BOLD}SUPERTREND STOP LOSS INFO{Colors.RESET}")
    print(f"{Colors.CYAN}{'═'*60}{Colors.RESET}")
    print(f"Direction: {dir_color}{dir_text}{Colors.RESET}")
    print(f"Current Price: {Colors.YELLOW}{current_price:.5f}{Colors.RESET}")
    print(f"ST Entry Value: {st_value:.5f}")
    print(f"ST Exit Value: {Colors.BOLD}{st_exit:.5f}{Colors.RESET}")
    print(f"Trend Extreme SL: {Colors.MAGENTA}{trend_sl:.5f}{Colors.RESET}")
    print(f"Distance to SL: ${abs(current_price - st_exit):.2f}")
    print(f"{Colors.CYAN}{'═'*60}{Colors.RESET}\n")

def check_sideways_candle_exit(position, closed_candle, st_angle):
    """Exit if market is SIDEWAYS AND candle color contradicts position"""
    
    # Check if market is sideways (angle = 0°)
    is_sideways = (st_angle == 0.0)
    
    if not is_sideways:
        return False, None  # Market is trending, don't exit on candle color
    
    # Market is sideways - check candle color
    closed_color = "GREEN" if closed_candle['close'] > closed_candle['open'] else "RED"
    pos_type = "BUY" if position.type == 0 else "SELL"
    
    # BUY: Exit on RED candle in sideways market
    if pos_type == "BUY" and closed_color == "RED":
        return True, "Sideways + Red Candle"
    
    # SELL: Exit on GREEN candle in sideways market
    elif pos_type == "SELL" and closed_color == "GREEN":
        return True, "Sideways + Green Candle"
    
    return False, None


def check_target_profit(pos, current_price, logger):
    """Exit when price moves $10 in profit direction."""
    pos_ticket = pos.ticket
    pos_type = "BUY" if pos.type == 0 else "SELL"
    entry_price = pos.price_open

    if pos_ticket in logger.target_profit_hit:
        return False, None

    price_movement = (current_price - entry_price) if pos_type == "BUY" else (entry_price - current_price)

    if price_movement >= 10.0:
        logger.target_profit_hit[pos_ticket] = True
        return True, f"$10 Target Profit (Entry: {entry_price:.2f} | Move: ${price_movement:.2f})"

    return False, None

def check_angle_requirements(st_angle, signal_type):
    """Check angle requirements: BUY needs +15°, SELL needs -15°"""
    if signal_type == "BUY":
        return st_angle >= 60.0  # Minimum +15 degrees for BUY
    elif signal_type == "SELL":
        return st_angle <= -60.0  # Minimum -15 degrees for SELL

    else:
        return False
def check_3dollar_stoploss(pos, current_price):
    """Exit if trade moves $3 against entry price."""
    pos_type = "BUY" if pos.type == 0 else "SELL"
    entry_price = pos.price_open

    adverse_movement = (entry_price - current_price) if pos_type == "BUY" else (current_price - entry_price)

    if adverse_movement >= 3.0:
        return True, f"$3 Stop Loss (Entry: {entry_price:.2f} | Loss: ${adverse_movement:.2f})"

    return False, None


def check_profit_protection(pos, current_price, logger):
    """Exit when profit retraces 50% from peak. Activates only after $5 peak profit."""
    pos_ticket = pos.ticket
    pos_type = "BUY" if pos.type == 0 else "SELL"
    entry_price = pos.price_open

    current_profit = (current_price - entry_price) if pos_type == "BUY" else (entry_price - current_price)

    # Track peak profit
    if pos_ticket not in logger.highest_profit_per_position:
        logger.highest_profit_per_position[pos_ticket] = current_profit
    elif current_profit > logger.highest_profit_per_position[pos_ticket]:
        logger.highest_profit_per_position[pos_ticket] = current_profit

    peak_profit = logger.highest_profit_per_position[pos_ticket]

    # Only activate after $5 minimum peak
    if peak_profit < 5.0:
        return False, None

    # Fire if profit retraced 50% from peak
    if current_profit <= peak_profit * 0.50:
        return True, f"Profit Protection (Peak: ${peak_profit:.2f} → Now: ${current_profit:.2f})"

    return False, None

#def check_candle_supertrend_conflict_exit(pos, current_candle_color, supertrend_direction, current_price):
    #"""Exit if candle color conflicts with SuperTrend direction AND position is $3 in loss."""
    #pos_type = "BUY" if pos.type == 0 else "SELL"
    #loss = (pos.price_open - current_price) if pos_type == "BUY" else (current_price - pos.price_open)

    #if loss < 3.0:
        #return False, None

    # GREEN candle during BEARISH SuperTrend, or RED candle during BULLISH SuperTrend
    #conflict = (current_candle_color == "GREEN" and supertrend_direction == -1) or \
               #(current_candle_color == "RED" and supertrend_direction == 1)

    #if conflict:
        #st_text = "BEAR" if supertrend_direction == -1 else "BULL"
        #return True, f"Candle+ST Conflict Exit ({current_candle_color} candle vs {st_text} ST, Loss: ${loss:.2f})"

    #return False, None



def get_candle_age_seconds(current_candle_time):
    """Calculate how many seconds have passed since candle opened"""
    import time
    current_timestamp = time.time()
    candle_timestamp = current_candle_time
    age_seconds = current_timestamp - candle_timestamp
    return age_seconds


def print_one_liner(time_display, tick_count, current_price, candle_color,
                    ema9, ema21, rsi, status, pl_value=None, trailing_sl=None):
    price_color = Colors.YELLOW
    candle_col = Colors.GREEN if candle_color == "GREEN" else Colors.RED
    ema_col = Colors.GREEN if ema9 > ema21 else Colors.RED
    status_col = Colors.GREEN if "POSITION" in status else Colors.CYAN

    pl_text = ""
    if pl_value is not None:
        pl_col = Colors.GREEN if pl_value >= 0 else Colors.RED
        pl_text = f" | P/L: {pl_col}${pl_value:.2f}{Colors.RESET}"

    sl_text = f" | TSL: {Colors.MAGENTA}{trailing_sl:.5f}{Colors.RESET}" if trailing_sl else ""

    print(f"[{time_display}] {Colors.CYAN}Tick#{tick_count}{Colors.RESET} | "
          f"Price: {price_color}{current_price:.5f}{Colors.RESET} | "
          f"Candle: {candle_col}{candle_color}{Colors.RESET} | "
          f"RSI: {rsi:.1f} | "
          f"EMA9: {ema_col}{ema9:.2f}{Colors.RESET} | "
          f"EMA21: {ema21:.2f} | "
          f"Status: {status_col}{status}{Colors.RESET}{pl_text}{sl_text}")




def print_trade_entry(time_display, pos_type, ticket, entry_price, volume, stop_loss, 
                     rsi, st_direction, angle, candle_color, capital, trades_today):
    """Print trade entry block"""
    print(f"\n{Colors.CYAN}╔{'═'*60}╗{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.BOLD}{'TRADE ENTERED'.center(60)}{Colors.RESET}{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}╠{'═'*60}╣{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET} Time: {time_display} | Type: {Colors.BOLD}{pos_type}{Colors.RESET} | Ticket: #{ticket}".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET} Entry: {entry_price:.5f} | Volume: {volume} | SL: {stop_loss:.5f}".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}╠{'═'*60}╣{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET} Entry Conditions: RSI✓ ST✓ Angle✓ Candle✓".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}╠{'═'*60}╣{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET} Capital: ${capital:.2f} | Trades Today: {trades_today}".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}╚{'═'*60}╝{Colors.RESET}\n")


def print_trade_exit(time_display, pos_type, ticket, entry_price, exit_price, duration, 
                    profit_loss, exit_reasons, total_trades, win_rate, wins, losses, capital):
    """Print trade exit block with colorful exit reasons"""
    # P/L color
    pl_col = Colors.GREEN if profit_loss >= 0 else Colors.RED
    pl_text = f"PROFIT: ${profit_loss:.2f} (WIN)" if profit_loss >= 0 else f"LOSS: ${profit_loss:.2f} (LOSS)"
    
    # Exit reason colors
    reason_colors = {
        "SuperTrend SL Cross": Colors.MAGENTA,
        "Red Candle Closed": Colors.RED,
        "Green Candle Closed": Colors.GREEN,
        "Trend Reversal": Colors.YELLOW,
        "Partial Profit": Colors.CYAN,
        "Market Sideways (0° Angle)": Colors.YELLOW,
        "$2 Trailing Stop": Colors.CYAN,
        "Profit Protection": Colors.GREEN, 
        "$3 Stop Loss": Colors.RED,
        "$10 Target Profit": Colors.GREEN,
        "Breakeven Exit": Colors.YELLOW,
        "Angle Weakness": Colors.YELLOW,
        "Candle+ST Conflict Exit": Colors.RED,
    }



    
    print(f"\n{Colors.CYAN}╔{'═'*60}╗{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.BOLD}{'TRADE EXITED'.center(60)}{Colors.RESET}{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}╠{'═'*60}╣{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET} Time: {time_display} | Type: {pos_type} | Ticket: #{ticket}".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET} Entry: {entry_price:.5f} | Exit: {exit_price:.5f} | Duration: {duration}".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}╠{'═'*60}╣{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET} {pl_col}{Colors.BOLD}{pl_text}{Colors.RESET}".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}╠{'═'*60}╣{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET} EXIT REASONS:".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    
    for reason in exit_reasons:
        reason_col = next((v for k, v in reason_colors.items() if reason.startswith(k)), Colors.RESET)
        print(f"{Colors.CYAN}║{Colors.RESET}   • {reason_col}{reason}{Colors.RESET}".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    
    print(f"{Colors.CYAN}╠{'═'*60}╣{Colors.RESET}")
    print(f"{Colors.CYAN}║{Colors.RESET} Session: {total_trades} Trades | {win_rate:.1f}% Win ({wins}W/{losses}L) | Capital: ${capital:.2f}".ljust(70) + f"{Colors.CYAN}║{Colors.RESET}")
    print(f"{Colors.CYAN}╚{'═'*60}╝{Colors.RESET}\n")


def print_candle_history_block(candle_history, current_time, logger):
    """Print 10-candle history block"""
    print(f"\n{Colors.CYAN}{'═'*80}{Colors.RESET}")
    print(f"{Colors.CYAN}[CANDLE HISTORY - {current_time.strftime('%H:%M:%S')}]{Colors.RESET}")
    print(f"{Colors.CYAN}{'═'*80}{Colors.RESET}")
    
    for idx, c in enumerate(candle_history, 1):
        dir_txt = "BULL" if c['direction'] == 1 else "BEAR"
        dir_col = Colors.GREEN if c['direction'] == 1 else Colors.RED
        
        angle = 0.0
        if idx > 1:
            angle = logger.calculate_supertrend_angle(c['supertrend'], candle_history[idx-2]['supertrend'])
        
        angle_sym = "↗" if angle > 0 else "↘" if angle < 0 else "→"
        angle_col = Colors.GREEN if angle > 0 else Colors.RED if angle < 0 else Colors.RESET
        
        print(f"#{idx:2d} {c['time']} | O:{c['open']:.2f} H:{c['high']:.2f} L:{c['low']:.2f} C:{c['close']:.2f} | "
              f"ST:{c['supertrend']:.2f}({dir_col}{dir_txt}{Colors.RESET}) {angle_col}{angle:+.1f}° {angle_sym}{Colors.RESET}")
    
    print(f"{Colors.CYAN}{'═'*80}{Colors.RESET}\n")


class TradeLogger:
    def __init__(self, session_capital=None):
        self.trades_executed = 0
        self.last_trade_time = None
        self.last_exit_time = None
        self.session_capital = session_capital  # Starting capital per session (configurable)
        self.current_capital = session_capital  # Current trading capital
        self.profits_reserved = 0.0    # Profits set aside
        self.candle_tick_count = 0     # Ticks within current candle
        self.entry_prices = {}         # Store entry prices for positions
        self.trades_this_candle = {}  # Track trades per candle
        self.candle_exit_occurred = {}  # Track if exit occurred in candle
        self.trade_volumes = {}      # Track volumes per trade
        self.previous_candle_structure = None  # Store previous candle structure
        self.candle_formation_progress = 0     # Track current candle formation %
        self.first_trade_exit_reason = {}  # Track exit reason per candle
        self.highest_profit_per_position = {}  # Track highest profit per position
        self.trailing_stop_per_position = {}   # Track trailing stop per position
        self.last_supertrend_direction = 0     # Track SuperTrend direction changes
        self.position_stop_loss = {}  # Track stop loss per position
        self.position_highest_price = {}  # Track highest price for BUY
        self.position_lowest_price = {}  # Track lowest price for SELL
        self.mt5_stop_loss = {}  # Track MT5 stop loss values
        self.trend_highest_price = 0.0      # Track highest price during bullish trend
        self.trend_lowest_price = 999999.0  # Track lowest price during bearish trend
        self.last_supertrend_direction = 0  # Track SuperTrend direction changes
        self.position_stop_loss = {}  # Track stop loss per position ticket
        self.trend_highest_price = 0.0
        self.trend_lowest_price = 999999.0
        self.supertrend_stop_loss = {}
        # Add these lines to TradeLogger.__init__ (around line 50)
        self.supertrend_up_high = {}      # Track highest price during bullish SuperTrend
        self.supertrend_down_low = {}     # Track lowest price during bearish SuperTrend
        self.high_water_mark_sl = {}      # High-water mark stop loss per position
        self.supertrend_stability_start = None  # When current direction started
        self.last_supertrend_direction = None   # Track direction changes
        self.trend_confirmed = False  # Track if current trend is confirmed
        self.position_entry_direction = {}  # Track SuperTrend direction when position was opened
        # Add these lines after line 50 in TradeLogger.__init__()
        self.trend_supertrend_values = []     # Store SuperTrend values for current trend
        self.current_trend_direction = 0      # Track current trend direction
        self.signal_confirmation = SignalConfirmation(required_confirmations=3)
        self.previous_supertrend_exit = None  # Track previous ST for angle

        self.executing_trade = False  # Global execution lock
        self.position_entry_prices = {}  # Track entry prices for $3 SL
        self.quick_exit_candles = {}  # Track candles with $3 SL exits
        self.trailing_sl_3 = {}  # Track $3 trailing stop loss per position
        self.trend_change_candle = {}    # Track candle when trend changed
        self.last_closed_candle_time = None  # Track last processed closed candle
        
        # ENHANCED PROFIT TRACKING
        self.total_profit = 0.0        # Total profit/loss accumulated
        self.winning_trades = 0        # Number of profitable trades
        self.losing_trades = 0         # Number of losing trades
        self.largest_win = 0.0         # Biggest single profit
        self.largest_loss = 0.0        # Biggest single loss
        self.trade_history = []        # Complete trade history
        self.position_entry_candle = {}  # Track entry candle time per position
        self.profit_milestone_tracker = {}  # Track $4 profit milestones per position
        self.position_highest_price = {}  # Track highest price for BUY (already exists)
        self.position_lowest_price = {}   # Track lowest price for SELL (already exists)
        self.trailing_stop_1dollar = {}   # Track $1 trailing stop per position
        self.trailing_stop_2dollar = {}
        self.breakeven_activated = {}  # Track if breakeven SL has been set per position
        self.target_profit_hit = {}  # Track $10 target profit per position
        self.breakeven_3dollar_activated = {}  # Track if $3 breakeven SL has been set per position

    





    def record_trade_for_candle(self, current_candle_time, position_ticket, volume):
        """Record trade for candle tracking"""
        if current_candle_time not in self.trades_this_candle:
            self.trades_this_candle[current_candle_time] = 0

        self.trades_this_candle[current_candle_time] += 1
        self.trade_volumes[position_ticket] = volume  # Store volume for this position

    # ADD THESE TWO NEW METHODS:
    def analyze_candle_structure(self, candle):
        """Analyze candle structure: bullish/bearish based on close vs open"""
        if candle['close'] > candle['open']:
            return "BULLISH"
        elif candle['close'] < candle['open']:
            return "BEARISH"
        else:
            return "DOJI"
    
    def calculate_candle_formation(self, current_candle, current_price):
        """Calculate how much of current candle has formed (0-100%)"""
        candle_range = current_candle['high'] - current_candle['low']
        if candle_range == 0:
            return 0
        price_movement = abs(current_price - current_candle['open'])
        formation_percentage = (price_movement / candle_range) * 100
        return min(formation_percentage, 100)  # Cap at 100%
    
    def calculate_candle_body_percentage(self, current_candle, current_price, previous_candle):
        """Calculate if current candle has built 50% of previous candle body"""
        prev_range = previous_candle['high'] - previous_candle['low']
        if prev_range == 0:
            return 0
        current_range = abs(current_price - current_candle['open'])
        return (current_range / prev_range) * 100
    
    def calculate_volume(self, current_price):
        if not current_price or not self.session_capital:
            return 0
        
        effective_capital = min(self.session_capital, 5000.0)
        
        volume = effective_capital / current_price
        volume = round(volume, 2)
        
        if volume < 0.01:
            return 0
        
        return volume

        
    def log_trade(self, signal, price, volume):
        self.trades_executed += 1
        self.last_trade_time = datetime.now()
        
    def log_exit(self, profit_loss=0):
        self.last_exit_time = datetime.now()

        print(f"[DEBUG] Exit P/L: {profit_loss:.2f} | Capital Before: {self.current_capital:.2f}")  # ADD THIS
        
        # ENHANCED PROFIT CAPTURE
        self.total_profit += profit_loss
        
        if profit_loss > 0:
            self.winning_trades += 1
            self.profits_reserved += profit_loss  # Set profits aside
            if profit_loss > self.largest_win:
                self.largest_win = profit_loss
        else:
            self.losing_trades += 1
            self.current_capital += profit_loss   # Deduct losses from capital
            if profit_loss < self.largest_loss:
                self.largest_loss = profit_loss
        print(f"[DEBUG] Capital After: {self.current_capital:.2f}")  # ADD THIS
        
        # Store trade in history
        self.trade_history.append({
            'time': self.last_exit_time,
            'profit_loss': profit_loss,
            'type': 'WIN' if profit_loss > 0 else 'LOSS'
        })
            
    def can_trade(self, cooldown_seconds=60):
        if self.last_exit_time is None:
            return True
        return (datetime.now() - self.last_exit_time).total_seconds() > cooldown_seconds
        
    def get_stats(self):
        win_rate = (self.winning_trades / (self.winning_trades + self.losing_trades) * 100) if (self.winning_trades + self.losing_trades) > 0 else 0
        return f"\nPROFIT CAPTURE STATISTICS:\n   Total Trades: {self.trades_executed}\n   Total Profit: ${self.total_profit:.2f}\n   Win Rate: {win_rate:.1f}% ({self.winning_trades}W/{self.losing_trades}L)\n   Largest Win: ${self.largest_win:.2f}\n   Largest Loss: ${self.largest_loss:.2f}\n   Capital: ${self.current_capital:.2f}\n   Profits Reserved: ${self.profits_reserved:.2f}\n   Last Trade: {self.last_trade_time or 'None'}"
    
    def check_supertrend_stability(self, current_direction, current_candle_time):
        """Check if SuperTrend has been stable for 2+ consecutive candles"""
        
        # Initialize history list if not exists
        if not hasattr(self, 'candle_history'):
            self.candle_history = []
            self.last_processed_candle = None
        
        # Only process each candle once
        if self.last_processed_candle != current_candle_time:
            self.candle_history.append({'time': current_candle_time, 'direction': current_direction})
            if len(self.candle_history) > 10:
                self.candle_history.pop(0)
            self.last_processed_candle = current_candle_time
        
        # Count consecutive candles with same direction from end
        if len(self.candle_history) < 2:
            return False, "1st candle"
        
        count = 1
        for i in range(len(self.candle_history) - 2, -1, -1):
            if self.candle_history[i]['direction'] == current_direction:
                count += 1
            else:
                break
        
        if count >= 1:
            return True, f"{count} candles"
        else:
            return False, "1st candle"


    def can_enter_new_trade(self, current_candle_time, symbol="XAUUSD"):
        """Check if we can enter a new trade - no position limit"""
        # Only check if there's an existing position
        existing_positions = mt5.positions_get(symbol=symbol)
        if existing_positions and len(existing_positions) > 0:
            return False  # Only 1 position at a time
        
        return True  # Allow entry anytime conditions are met


    
    def calculate_trend_extreme_sl(self, supertrend_direction, supertrend_value):
        """Calculate stop loss based on trend extremes"""
        # Reset when trend changes
        if self.current_trend_direction != supertrend_direction:
            self.current_trend_direction = supertrend_direction
            self.trend_supertrend_values = [supertrend_value]
        else:
            # Add current value to trend sequence
            self.trend_supertrend_values.append(supertrend_value)
        
        # Return extreme based on direction
        if supertrend_direction == 1:  # Bullish - highest value
            return max(self.trend_supertrend_values)
        else:  # Bearish - lowest value
            return min(self.trend_supertrend_values)
                
            
    def calculate_supertrend_angle(self, current_st, previous_st):
        """Calculate SuperTrend angle in degrees"""
        import math
        if previous_st == 0:
            return 0.0
        vertical_change = current_st - previous_st
        angle_radians = math.atan(vertical_change)
        return round(math.degrees(angle_radians), 2)

    def calculate_price_momentum_angle(self, current_price, candle_open):
        """Calculate angle based on price movement from candle open - updates every tick"""
        import math
        if candle_open == 0:
            return 0.0
        price_change = current_price - candle_open
        normalized_change = price_change / candle_open * 100
        angle_radians = math.atan(normalized_change)
        return round(math.degrees(angle_radians), 2)
    
    def calculate_supertrend_slope_angle(self, st_current, st_previous):
        import math
        if st_previous == 0:
            return 0.0
        slope = st_current - st_previous
        return round(math.degrees(math.atan(slope)), 2)

    # ADD THIS METHOD RIGHT HERE:
    def calculate_realtime_supertrend_angle(self, current_price, strategy):
        """Calculate SuperTrend angle using current live price within the candle"""
        import pandas as pd
    
        try:
            # Get recent rates including current forming candle
            rates = mt5.copy_rates_from_pos("XAUUSD", mt5.TIMEFRAME_M5, 0, 20)
            if rates is None or len(rates) < 10:
                return 0.0
        
            # Create DataFrame
            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
        
            # Update current candle with live price data
            current_candle_idx = len(df) - 1
            df.loc[current_candle_idx, 'close'] = current_price
            df.loc[current_candle_idx, 'high'] = max(df.iloc[current_candle_idx]['high'], current_price)
            df.loc[current_candle_idx, 'low'] = min(df.iloc[current_candle_idx]['low'], current_price)
        
            # Calculate SuperTrend with live data
            st_data = strategy.calculate_supertrend_pinescript(df, atr_length=10, atr_multiplier=0.9, smoothing_period=1)
        
            if len(st_data) >= 2:
                current_st = st_data['supertrend'].iloc[-1]
                previous_st = st_data['supertrend'].iloc[-2]
                angle = self.calculate_supertrend_angle(current_st, previous_st)
            
                # Debug output every 50 ticks
                if not hasattr(self, 'angle_debug_counter'):
                    self.angle_debug_counter = 0
                self.angle_debug_counter += 1
                
                if self.angle_debug_counter % 50 == 0:
                    print(f"[REALTIME_ANGLE] Live ST: {current_st:.5f} | Prev ST: {previous_st:.5f} | Angle: {angle:+.1f}°")
            
                return angle
        
            return 0.0
        
        except Exception as e:
            if not hasattr(self, 'angle_error_count'):
                self.angle_error_count = 0
            self.angle_error_count += 1
        
            if self.angle_error_count % 100 == 1:
                print(f"[ANGLE_ERROR] Real-time calculation failed: {e}")
            return 0.0

    
    def detect_first_candle_of_trend(self, current_direction, current_candle_time):
        """Detect if this is the first candle of a new trend"""
        
        # Initialize tracking variables if not exists
        if not hasattr(self, 'previous_supertrend_direction'):
            self.previous_supertrend_direction = current_direction
            self.trend_start_candle = current_candle_time
            self.last_direction_check_candle = None
            return False
        
        # Only check once per candle (not per tick)
        if self.last_direction_check_candle != current_candle_time:
            self.last_direction_check_candle = current_candle_time
            
            # Check for direction change
            if current_direction != self.previous_supertrend_direction:
                print(f"{Colors.MAGENTA}[NEW_TREND] Direction changed: {self.previous_supertrend_direction} → {current_direction}{Colors.RESET}")
                self.previous_supertrend_direction = current_direction
                self.trend_start_candle = current_candle_time
                return True
        
        # Check if we're still in first candle of current trend
        return current_candle_time == self.trend_start_candle

            
def complete_entry_analysis():
    load_dotenv(os.path.join(os.path.dirname(__file__), 'trade_backend', '.env'))

    
    # MT5 Connection
    mt5_path = os.getenv("MT5_PATH")
    mt5_login = int(os.getenv("MT5_LOGIN"))
    mt5_pass = os.getenv("MT5_PASSWORD")
    mt5_server = os.getenv("MT5_SERVER")
    
    if not mt5.initialize(path=mt5_path, login=mt5_login, password=mt5_pass, server=mt5_server):
        print(f"MT5 initialization failed: {mt5.last_error()}")
        return
    
    # Initialize components
    symbol = "XAUUSD"
    strategy = EnhancedTradingStrategy(symbol, "M5")
           
    # ADD THESE LINES HERE:
    print("\n" + "="*80)
    print("[LAST 10 CANDLES WITH SUPERTREND]")
    print("="*80)
    candle_history = fetch_candle_history(symbol, strategy, 10)
    for idx, c in enumerate(candle_history, 1):
        dir_txt = "BULL" if c['direction'] == 1 else "BEAR"
        print(f"#{idx} {c['time']} | O:{c['open']:.2f} H:{c['high']:.2f} L:{c['low']:.2f} C:{c['close']:.2f} | ST:{c['supertrend']:.2f} ({dir_txt})")
    print("="*80 + "\n")
        
    # Default capital set to $5,000
    account_info = mt5.account_info()
    session_capital = account_info.balance if account_info else 1000.0
    logger = TradeLogger(session_capital)
    
    print(f"\n[ZERO-LATENCY TRADING SYSTEM ACTIVE]")
    print(f"Symbol: {symbol}")
    print(f"Entry: BUY(RSI>50 + Green candle + EMA9>EMA21) | SELL(RSI<50 + Red candle + EMA9<EMA21)")
    print(f"Exit: $3 SL | $10 TP | $2 Trailing (after $5 profit) | Breakeven (after $3 profit)")
    print("="*80)
    
    tick_count = 0
    start_time = time.time()
    last_tick_time = None
    
    try:
        while True:
            tick_count += 1
            current_time = datetime.now(pytz.timezone('Europe/Athens'))  # EET timezone
            time_display = current_time.strftime("%H:%M:%S.%f")[:-3]
            
            # Get fresh tick data
            tick = mt5.symbol_info_tick(symbol)
            if tick and (last_tick_time is None or tick.time != last_tick_time):
                last_tick_time = tick.time
                # Use bid price as current price (more reliable than tick.last)
                current_price = tick.bid if tick.bid > 0 else tick.ask
                
                # Get market data
                market_data = {
                    'bid': tick.bid,
                    'ask': tick.ask,
                    'spread': tick.ask - tick.bid,
                    'volume': tick.volume
                }
                
                # Get analysis data
                analysis = strategy.analyze_timeframe("M5")
                if analysis:
                    # Extract indicators with SuperTrend prominence
                    rsi = analysis.get('rsi', 0)
                    ema9 = analysis.get('ema9', 0)
                    ema21 = analysis.get('ema21', 0)
                    supertrend_direction = analysis.get('supertrend_direction', 0)  # Entry signal direction
                    supertrend_value = analysis.get('supertrend_value', 0)  # Entry SuperTrend (Period=10, Multiplier=0.9)
                    supertrend_exit_value = analysis.get('supertrend_exit_value', 0)  # Exit SuperTrend (Period=10, Multiplier=0.9)
                    atr = analysis.get('atr', 0)

                    st_angle = logger.calculate_supertrend_slope_angle(supertrend_value, getattr(logger, 'prev_supertrend_value', supertrend_value))

                                      
                     
                    # Get current candle data + intra-candle analysis
                    
                    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 3)
                    if rates is not None and len(rates) >= 2:
                        current_candle = rates[-1]
                        previous_candle = rates[-2]
                        
                        # INTRA-CANDLE ANALYSIS: Track ticks within candle
                        current_candle_time = current_candle['time']
                        

                        

                        if not hasattr(logger, 'last_candle_time') or logger.last_candle_time != current_candle_time:
                            logger.last_candle_time = current_candle_time
                            logger.prev_supertrend_value = getattr(logger, 'current_supertrend_value', supertrend_value)  # ADD
                            logger.candle_tick_count = 0  # Reset for new candle
                            # Only clear signal history, don't trigger reset cycle
                            logger.signal_confirmation.signal_history = []
                            logger.signal_confirmation.direction_changes = []

                            

                            

                            # Dynamic candle history update every minute
                            if tick_count > 50:
                                candle_history = fetch_candle_history(symbol, strategy, 10)
                                print_candle_history_block(candle_history, current_time, logger)
                                
                                

                        logger.candle_tick_count += 1

                        
                        # STRUCTURE ANALYSIS: Previous candle structure
                        prev_structure = logger.analyze_candle_structure(previous_candle)
                        logger.previous_candle_structure = prev_structure
                        
                       
                        
                        # ENHANCED ENTRY LOGIC: RSI + SuperTrend + Angle + Candle Color (ALL parallel)
                        structure_signal = None
                        current_candle_color = "GREEN" if current_price > current_candle['open'] else "RED"

                        # Define existing_positions early
                        existing_positions = mt5.positions_get(symbol=symbol)

                        if rsi > 50 and current_candle_color == "GREEN" and ema9 > ema21:
                            structure_signal = "BUY"
                        elif rsi < 50 and current_candle_color == "RED" and ema9 < ema21:
                            structure_signal = "SELL"
                                            
                        # Current candle direction confirmation (already covered by candle color check)
                        structure_confirmed = structure_signal is not None

                        
                        # ENTRY SIGNAL: Immediate - RSI + Candle Color + EMA only
                        if not existing_positions and structure_signal:
                            signal = structure_signal
                            entry_conditions_met = True
                            print(f"[SIGNAL] {signal} | RSI:{rsi:.1f} | Candle:{current_candle_color} | EMA9:{ema9:.2f} | EMA21:{ema21:.2f}")
                        else:
                            signal = "NONE"
                            entry_conditions_met = False



                        # Get symbol info for tick size
                        symbol_info = mt5.symbol_info(symbol)
                        tick_size = symbol_info.trade_tick_size if symbol_info else 0.01
                        
                        # ENTRY EXECUTION: Simplified validation
                        if entry_conditions_met and current_price > 0 and atr > 0:
                            volume = logger.calculate_volume(current_price)
                            if volume > 0:
                                order_ready = True
                                if signal == "BUY":
                                    entry_price = market_data['ask'] + tick_size
                                else:  # SELL
                                    entry_price = market_data['bid'] - tick_size
                            else:
                                order_ready = False
                                entry_price = 0
                        else:
                            order_ready = False
                            entry_price = 0
                                                                        
                        

                        
                        # Calculate candle age
                        candle_age = get_candle_age_seconds(current_candle_time)

                        # Angle requirement already checked in signal confirmation
                        all_systems_go = order_ready and entry_conditions_met
                        
                        # ========== PARALLEL EXIT CONDITIONS ==========
                        existing_positions = mt5.positions_get(symbol=symbol)
                        if existing_positions:
                                
                                for pos in existing_positions:
                                    pos_type = "BUY" if pos.type == 0 else "SELL"
                                    pos_ticket = pos.ticket
                                    
                                    # Collect ALL exit reasons in parallel
                                    exit_reasons = []
                                    
                                    # === PARALLEL EXIT CONDITIONS ===
                                    entry_price = pos.price_open
                                    price_movement = (current_price - entry_price) if pos_type == "BUY" else (entry_price - current_price)
                                    # Check if we've reached $10 profit milestone
                                    protection_mode = price_movement >= 5.0

                                    # === AFTER $10: $2 Trailing Stop ===
                                    if protection_mode:
                                        # Initialize trailing stop dictionary if not exists
                                        if not hasattr(logger, 'trailing_stop_2dollar'):
                                            logger.trailing_stop_2dollar = {}
                                        
                                        if pos_type == "BUY":
                                            # Calculate trailing stop: $2 below current price
                                            new_trailing_stop = current_price - 2.0
                                            
                                            # Initialize or update (only move UP, never down)
                                            if pos_ticket not in logger.trailing_stop_2dollar:
                                                logger.trailing_stop_2dollar[pos_ticket] = new_trailing_stop
                                                print(f"{Colors.CYAN}[TRAILING ACTIVATED] ${new_trailing_stop:.5f} (Profit: ${price_movement:.2f}){Colors.RESET}")
                                                update_mt5_stop_loss(pos_ticket, new_trailing_stop)
                                            elif new_trailing_stop > logger.trailing_stop_2dollar[pos_ticket]:
                                                logger.trailing_stop_2dollar[pos_ticket] = new_trailing_stop
                                                print(f"{Colors.CYAN}[TRAILING MOVED UP] ${new_trailing_stop:.5f} (Profit: ${price_movement:.2f}){Colors.RESET}")
                                                update_mt5_stop_loss(pos_ticket, new_trailing_stop)
                                            
                                            # Check if price hit trailing stop
                                            if current_price <= logger.trailing_stop_2dollar[pos_ticket]:
                                                exit_reasons.append(f"$2 Trailing Stop (${price_movement:.2f} profit)")

                                        
                                        else:  # SELL
                                            # Calculate trailing stop: $2 above current price
                                            new_trailing_stop = current_price + 2.0
                                            
                                            # Initialize or update (only move DOWN, never up)
                                            if pos_ticket not in logger.trailing_stop_2dollar:
                                                logger.trailing_stop_2dollar[pos_ticket] = new_trailing_stop
                                                print(f"{Colors.CYAN}[TRAILING ACTIVATED] ${new_trailing_stop:.5f} (Profit: ${price_movement:.2f}){Colors.RESET}")
                                                update_mt5_stop_loss(pos_ticket, new_trailing_stop)
                                            elif new_trailing_stop < logger.trailing_stop_2dollar[pos_ticket]:
                                                logger.trailing_stop_2dollar[pos_ticket] = new_trailing_stop
                                                print(f"{Colors.CYAN}[TRAILING MOVED DOWN] ${new_trailing_stop:.5f} (Profit: ${price_movement:.2f}){Colors.RESET}")
                                                update_mt5_stop_loss(pos_ticket, new_trailing_stop)

                                            
                                            # Check if price hit trailing stop
                                            if current_price >= logger.trailing_stop_2dollar[pos_ticket]:
                                                exit_reasons.append(f"$2 Trailing Stop (${price_movement:.2f} profit)")


                                    
                                    # === EXIT CONDITION 1.5: $1 Loss Stop Loss ===
                                    #if price_movement <= -1.0:
                                        #exit_reasons.append("$1 Loss Stop Loss")
                                    
                                    # === EXIT CONDITION 1.6: $1 Trailing Stop Loss ===
                                    # Track highest/lowest price and exit if price retraces $1 from best
                                    #if pos_type == "BUY":
                                        # Update highest price
                                        #if pos_ticket not in logger.position_highest_price:
                                            #logger.position_highest_price[pos_ticket] = entry_price
                                        #if current_price > logger.position_highest_price[pos_ticket]:
                                            #logger.position_highest_price[pos_ticket] = current_price
                                        
                                        # Check if price dropped $1 from highest
                                        #highest = logger.position_highest_price[pos_ticket]
                                        #if current_price <= highest - 1.0:
                                            #exit_reasons.append(f"$1 Trailing Stop (High: ${highest:.2f})")
                                            
                                    #else:  # SELL
                                        # Update lowest price
                                        #if pos_ticket not in logger.position_lowest_price:
                                            #logger.position_lowest_price[pos_ticket] = entry_price
                                        #if current_price < logger.position_lowest_price[pos_ticket]:
                                            #logger.position_lowest_price[pos_ticket] = current_price
                                        
                                        # Check if price rose $1 from lowest
                                        #lowest = logger.position_lowest_price[pos_ticket]
                                        #if current_price >= lowest + 1.0:
                                            #exit_reasons.append(f"$1 Trailing Stop (Low: ${lowest:.2f})")
                                    
                                    # === EXIT CONDITION: $3 Stop Loss ===
                                    sl_exit, sl_reason = check_3dollar_stoploss(pos, current_price)
                                    if sl_exit:
                                        exit_reasons.append(sl_reason)

                                    # === BREAKEVEN: Move SL to entry after $3 profit ===
                                    if price_movement >= 3.0 and pos_ticket not in logger.breakeven_3dollar_activated:
                                        logger.breakeven_3dollar_activated[pos_ticket] = entry_price
                                        update_mt5_stop_loss(pos_ticket, entry_price)
                                        print(f"{Colors.GREEN}[BREAKEVEN SET] SL moved to entry {entry_price:.2f} after ${price_movement:.2f} profit{Colors.RESET}")

                                    # === EXIT: Price returned to breakeven (entry price) ===
                                    if pos_ticket in logger.breakeven_3dollar_activated:
                                        be_price = logger.breakeven_3dollar_activated[pos_ticket]
                                        be_hit = (pos_type == "BUY" and current_price <= be_price) or \
                                                 (pos_type == "SELL" and current_price >= be_price)
                                        if be_hit:
                                            exit_reasons.append(f"Breakeven Exit (Entry: {be_price:.2f})")
                                            print(f"{Colors.YELLOW}[BREAKEVEN EXIT] Price returned to entry {be_price:.2f}{Colors.RESET}")

                                    # === EXIT CONDITION: Profit Protection ===
                                    pp_exit, pp_reason = check_profit_protection(pos, current_price, logger)
                                    if pp_exit:
                                        exit_reasons.append(pp_reason)

                                    # === EXIT CONDITION: $10 Target Profit ===
                                    tp_exit, tp_reason = check_target_profit(pos, current_price, logger)
                                    if tp_exit:
                                        exit_reasons.append(tp_reason)
                                        print(f"{Colors.GREEN}[TARGET HIT] $10 profit reached - closing position{Colors.RESET}")

                                    # === EXIT CONDITION: Candle vs SuperTrend Conflict + $3 Loss ===
                                    #conflict_exit, conflict_reason = check_candle_supertrend_conflict_exit(
                                        #pos, current_candle_color, supertrend_direction, current_price
                                    #)
                                    #if conflict_exit:
                                        #exit_reasons.append(conflict_reason)
                                        #print(f"{Colors.RED}[CONFLICT_EXIT] {conflict_reason}{Colors.RESET}")


                                   
                                    # === EXIT CONDITION 2: Trend Reversal ===
                                    if pos_ticket in logger.position_entry_direction:
                                        entry_direction = logger.position_entry_direction[pos_ticket]
                                        if entry_direction != supertrend_direction:
                                            if pos_ticket not in logger.trend_change_candle:
                                                logger.trend_change_candle[pos_ticket] = current_candle_time
                                                print(f"[TREND_CHANGE] Detected for position {pos_ticket}. Waiting for candle close...")
                                            elif logger.trend_change_candle[pos_ticket] != current_candle_time:
                                                exit_reasons.append("Trend Reversal")
                                        else:
                                            if pos_ticket in logger.trend_change_candle:
                                                del logger.trend_change_candle[pos_ticket]
                                                print(f"[TREND_RESTORED] Position {pos_ticket} trend back to original")
                                   
                                   
                                                                        
                                    # === EXIT CONDITION 3: SuperTrend SL Cross ===
                                    realtime_exit_direction = 1 if current_price > supertrend_exit_value else -1
                                    stop_loss_value = logger.calculate_trend_extreme_sl(realtime_exit_direction, supertrend_exit_value)
                                                                                                                                                
                                    # Update trailing SL
                                    if pos_type == "BUY":
                                        if stop_loss_value < current_price and stop_loss_value > logger.high_water_mark_sl.get(pos_ticket, 0):
                                            logger.high_water_mark_sl[pos_ticket] = stop_loss_value
                                    else:
                                        if stop_loss_value > current_price and (pos_ticket not in logger.high_water_mark_sl or stop_loss_value < logger.high_water_mark_sl[pos_ticket]):
                                            logger.high_water_mark_sl[pos_ticket] = stop_loss_value

                                    
                                    # Check SL cross
                                    if pos_type == "BUY" and current_price <= stop_loss_value:
                                        exit_reasons.append("SuperTrend SL Cross")
                                    elif pos_type == "SELL" and current_price >= stop_loss_value:
                                        exit_reasons.append("SuperTrend SL Cross")
                                    
                                    # === EXECUTE EXIT IF ANY CONDITION MET ===
                                    if exit_reasons:
                                        close_type = mt5.ORDER_TYPE_SELL if pos_type == "BUY" else mt5.ORDER_TYPE_BUY
                                        result = mt5.order_send({
                                            'action': mt5.TRADE_ACTION_DEAL,
                                            'symbol': symbol,
                                            'volume': pos.volume,
                                            'type': close_type,
                                            'position': pos.ticket,
                                            'type_filling': mt5.ORDER_FILLING_RETURN,
                                            'magic': 123456
                                        })
                                        
                                        # Calculate duration
                                        entry_time = logger.last_trade_time
                                        exit_time = datetime.now()
                                        duration = str(exit_time - entry_time).split('.')[0] if entry_time else "Unknown"
                                        
                                        # Calculate win rate
                                        total = logger.winning_trades + logger.losing_trades + (1 if pos.profit >= 0 else 0)
                                        win_rate = ((logger.winning_trades + (1 if pos.profit >= 0 else 0)) / total * 100) if total > 0 else 0
                                        
                                        # Print trade exit block
                                        print_trade_exit(
                                            time_display=time_display,
                                            pos_type=pos_type,
                                            ticket=pos_ticket,
                                            entry_price=pos.price_open,
                                            exit_price=current_price,
                                            duration=duration,
                                            profit_loss=pos.profit,
                                            exit_reasons=exit_reasons,
                                            total_trades=logger.trades_executed,
                                            win_rate=win_rate,
                                            wins=logger.winning_trades + (1 if pos.profit >= 0 else 0),
                                            losses=logger.losing_trades + (1 if pos.profit < 0 else 0),
                                            capital=logger.current_capital + pos.profit,
                                        )
                                        
                                    
                                        logger.log_exit(pos.profit)
                                        
                                        # Only reset for trailing stop exits, not trend reversals or other exits
                                        if any("Trailing Stop" in reason for reason in exit_reasons):
                                            logger.signal_confirmation.reset()
                                            print(f"{Colors.CYAN}[TRAILING_EXIT_RESET] Reset required after trailing stop exit{Colors.RESET}")
                                        else:
                                            print(f"{Colors.GREEN}[NORMAL_EXIT] No reset required - ready for immediate re-entry{Colors.RESET}")
                                        
                                        print(f"{Colors.CYAN}[EXIT_COMPLETE] Position closed - waiting for fresh signal cycle{Colors.RESET}")


                                        # Profit captured - wait for new entry conditions
                                        if any("Profit Milestone" in reason for reason in exit_reasons):
                                            print(f"\n{Colors.GREEN}✅ PROFIT CAPTURED: ${pos.profit:.2f} | Waiting for new entry signal...{Colors.RESET}\n")
                                       
                                        # Cleanup
                                        if pos_ticket in logger.high_water_mark_sl:
                                            del logger.high_water_mark_sl[pos_ticket]
                                        if pos_ticket in logger.position_entry_prices:
                                            del logger.position_entry_prices[pos_ticket]
                                        if pos_ticket in logger.trend_change_candle:
                                            del logger.trend_change_candle[pos_ticket]
                                        if pos_ticket in logger.position_entry_candle:
                                            del logger.position_entry_candle[pos_ticket]
                                        if pos_ticket in logger.profit_milestone_tracker:
                                            del logger.profit_milestone_tracker[pos_ticket]
                                        if pos_ticket in logger.position_highest_price:  # ADD THIS
                                            del logger.position_highest_price[pos_ticket]
                                        if pos_ticket in logger.position_lowest_price:   # ADD THIS
                                            del logger.position_lowest_price[pos_ticket]
                                        if pos_ticket in logger.trailing_stop_2dollar:
                                            del logger.trailing_stop_2dollar[pos_ticket]
                                        if pos_ticket in logger.highest_profit_per_position:
                                            del logger.highest_profit_per_position[pos_ticket]
                                        if pos_ticket in logger.breakeven_activated:
                                            del logger.breakeven_activated[pos_ticket]
                                        if pos_ticket in logger.target_profit_hit:
                                            del logger.target_profit_hit[pos_ticket]
                                        if pos_ticket in logger.breakeven_3dollar_activated:
                                            del logger.breakeven_3dollar_activated[pos_ticket]



                                                                              
                                        break
                                
                                # Update last closed candle time AFTER all checks
                                if logger.last_closed_candle_time != current_candle_time:
                                    logger.last_closed_candle_time = current_candle_time



                                    
                                                             
                        # === DISPLAY ONE-LINER (EVERY TICK) ===
                        current_candle_color = "GREEN" if current_price > current_candle['open'] else "RED"

                       
                        if existing_positions:
                            status = "IN POSITION"
                            pl_value = existing_positions[0].profit
                        else:
                            status = "WAITING"
                            pl_value = None


                        # Get trailing SL if exists
                        trailing_sl_value = None
                        if existing_positions and existing_positions[0].ticket in logger.trailing_stop_2dollar:
                            trailing_sl_value = logger.trailing_stop_2dollar[existing_positions[0].ticket]

                        # Print one-liner
                        print_one_liner(
                            time_display=time_display,
                            tick_count=tick_count,
                            current_price=current_price,
                            candle_color=current_candle_color,
                            ema9=ema9,
                            ema21=ema21,
                            rsi=rsi,
                            status=status,
                            pl_value=pl_value,
                            trailing_sl=trailing_sl_value
                        )



                            
                        # EXECUTION: Enter when conditions met, only 1 position at a time
                        if all_systems_go and not existing_positions:

                            # GLOBAL LOCK: Prevent simultaneous execution
                            if logger.executing_trade:
                                continue
           
                            logger.executing_trade = True  # Lock immediately
    
                            try:
                                volume = logger.calculate_volume(current_price)
                                if volume > 0:
                                    result = mt5.order_send({
                                        'action': mt5.TRADE_ACTION_DEAL,
                                        'symbol': symbol,
                                        'volume': volume,
                                        'type': mt5.ORDER_TYPE_BUY if signal == 'BUY' else mt5.ORDER_TYPE_SELL,
                                        'type_filling': mt5.ORDER_FILLING_RETURN,
                                        'magic': 123456
                                })
            
                                if result and result.retcode == mt5.TRADE_RETCODE_DONE:
                                    logger.log_trade(signal, result.price, volume)
                                    
                                    # Get position details
                                    positions = mt5.positions_get(symbol=symbol)
                                    if positions:
                                        pos = positions[-1]
                                        pos_ticket = pos.ticket
                                        logger.position_entry_prices[pos_ticket] = result.price
                                        logger.position_entry_direction[pos_ticket] = supertrend_direction
                                        logger.position_entry_candle[pos_ticket] = current_candle_time
                                        
                                        # Print trade entry block
                                        print_trade_entry(
                                            time_display=time_display,
                                            pos_type=signal,
                                            ticket=pos_ticket,
                                            entry_price=result.price,
                                            volume=volume,
                                            stop_loss=supertrend_exit_value,
                                            rsi=rsi,
                                            st_direction=supertrend_direction,
                                            angle=st_angle,
                                            candle_color=current_candle_color,
                                            capital=logger.current_capital,
                                            trades_today=logger.trades_executed
                                        )


                


                                else:
                                    print(f"\n[TRADE FAILED] {signal} - {result.comment if result else 'Unknown'}")
                       
                            finally:
                                logger.executing_trade = False  # Always unlock

                        
                        logger.current_supertrend_value = supertrend_value
                        time.sleep(0)  # ZERO delay for absolute maximum speed

    except KeyboardInterrupt:
        print(f"\n\nSystem stopped by user")
        print(logger.get_stats())
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    complete_entry_analysis()