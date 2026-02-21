import pandas as pd
import numpy as np
import xgboost as xgb
import pandas_ta as ta
import ccxt
import time
import os
import json
import requests
import csv
import sqlite3
from datetime import datetime, timedelta
import random

# ==========================================
# 👹 SNIPER PROTOCOL V9 - SMC EVOLVED (DB + MONITORAGGIO AVANZATO)
# ==========================================

class SniperAgentV9:
    def __init__(self):
        # --- CONFIGURAZIONE ---
        self.telegram_token = '8524526621:AAHQhicgYChPk1iP-AylSqEM5JOZbimaa8c'
        self.chat_id = '6654522011'
        
        # Collegamento Binance Futures
        self.exchange = ccxt.binance({
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future'
            }
        })
        
        self.watchlist = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT', 'AVAX/USDT', 'NEAR/USDT', 'DOGE/USDT', 'TRX/USDT', 'HYPE/USDT']
        self.memory_file = "sniper_memory.json"
        self.trades_file = "active_trades.json"
        self.history_file = "trade_history.csv"
        self.offset_file = "telegram_offset.json"
        self.db_file = "trading_bot.db"  # Nuovo database SQLite
        
        # --- FILTRO DOMINANCE ---
        self.btc_dom_threshold = 1.5
        
        # --- TELEGRAM OFFSET ---
        self.load_telegram_offset()

        # --- CERVELLO IA (Bias Direzionale) ---
        self.models = {}  # Dizionario di modelli per simbolo
        
        # --- INIZIALIZZAZIONE MEMORIA ---
        self.signal_history = []
        self.active_trades = {}

        # --- STATO DEL MERCATO (REGIME) ---
        self.regime = None  # Verrà aggiornato periodicamente 
        
        # --- FILTRO VOLUMI DINAMICO ---
        self.vol_multiplier = 1.5  # valore di default (poi verrà aggiornato)

        # --- SAFETY SWITCH (CIRCUIT BREAKER) ---
        self.mode = 'normal'              # 'normal', 'aggressive', 'conservative'
        self.consecutive_losses = 0       # contatore perdite consecutive
        self.cooldown_until = None        # timestamp di fine cooldown (datetime o None)

        self.load_memory()
        self.load_active_trades()
        self.init_history_file()
        self.init_database()  # Nuovo: inizializza DB SQLite

    # [GESTIONE FILE E MEMORIA]
    def load_memory(self):
        if not os.path.exists(self.memory_file):
            with open(self.memory_file, 'w') as f: json.dump([], f)

    def load_active_trades(self):
        if os.path.exists(self.trades_file):
            try:
                with open(self.trades_file, 'r') as f:
                    self.active_trades = json.load(f)
            except Exception as e:
                print(f"Errore caricamento trades: {e}")
                self.active_trades = {}
        else:
            self.active_trades = {}

    def save_active_trades(self):
        with open(self.trades_file, 'w') as f:
            json.dump(self.active_trades, f, indent=4)

    def init_history_file(self):
        if not os.path.exists(self.history_file):
            with open(self.history_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["Date", "Asset", "Type", "Price", "Result"])

    # --- NUOVO: DATABASE SQLITE ---
    def init_database(self):
        """Crea le tabelle nel database SQLite se non esistono."""
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        # Tabella trades: memorizza le informazioni principali di ogni trade
        c.execute('''CREATE TABLE IF NOT EXISTS trades
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      symbol TEXT NOT NULL,
                      side TEXT NOT NULL,
                      entry_price REAL,
                      exit_price REAL,
                      status TEXT,  -- pending, active, closed
                      sl REAL,
                      tp1 REAL,
                      tp2 REAL,
                      tp3 REAL,
                      created_at TIMESTAMP,
                      updated_at TIMESTAMP,
                      closed_at TIMESTAMP,
                      result TEXT)''')
        # Tabella trade_events: traccia tutti gli eventi del trade (TP hit, SL, BE, etc.)
        c.execute('''CREATE TABLE IF NOT EXISTS trade_events
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      trade_id INTEGER,
                      event_type TEXT,  -- TP1_HIT, TP2_HIT, TP3_HIT, SL_HIT, BE_HIT, MANUAL_CLOSE, etc.
                      price REAL,
                      timestamp TIMESTAMP,
                      FOREIGN KEY(trade_id) REFERENCES trades(id))''')
        # Tabella logs: errori e informazioni di debug
        c.execute('''CREATE TABLE IF NOT EXISTS logs
                     (id INTEGER PRIMARY KEY AUTOINCREMENT,
                      level TEXT,
                      message TEXT,
                      timestamp TIMESTAMP)''')
        conn.commit()
        conn.close()

    def log_to_db(self, table, data):
        """Inserisce un record nel database."""
        try:
            conn = sqlite3.connect(self.db_file)
            c = conn.cursor()
            columns = ', '.join(data.keys())
            placeholders = ':' + ', :'.join(data.keys())
            query = f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"
            c.execute(query, data)
            conn.commit()
            conn.close()
        except Exception as e:
            self.log_error(f"DB insert error: {e}")

    def log_trade_event(self, trade_id, event_type, price):
        """Registra un evento di trade nel DB."""
        data = {
            'trade_id': trade_id,
            'event_type': event_type,
            'price': price,
            'timestamp': datetime.now().isoformat()
        }
        self.log_to_db('trade_events', data)

    def log_error(self, message):
        """Registra un errore nel DB."""
        data = {
            'level': 'ERROR',
            'message': message,
            'timestamp': datetime.now().isoformat()
        }
        self.log_to_db('logs', data)

    def save_to_history(self, symbol, event_type, price, trade_id=None):
        """Salva evento in CSV e nel DB (se trade_id fornito)."""
        # CSV
        try:
            with open(self.history_file, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, event_type, price, "CLOSED" if "SL" in event_type or "MANUAL" in event_type or "BE" in event_type or "MISSED" in event_type else "OPEN"])
        except Exception as e:
            print(f"Errore salvataggio history: {e}")
        # DB
        if trade_id:
            self.log_trade_event(trade_id, event_type, price)

    # --- GESTIONE TELEGRAM OFFSET ---
    def load_telegram_offset(self):
        try:
            if os.path.exists(self.offset_file):
                with open(self.offset_file, 'r') as f:
                    self.update_offset = json.load(f)
            else:
                self.update_offset = 0
        except Exception as e:
            print(f"Errore caricamento offset Telegram: {e}")
            self.update_offset = 0

    def save_telegram_offset(self):
        try:
            with open(self.offset_file, 'w') as f:
                json.dump(self.update_offset, f)
        except Exception as e:
            print(f"Errore salvataggio offset Telegram: {e}")

    # --- GESTIONE TELEGRAM ---
    def send_telegram(self, text, keyboard=None):
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            payload = {'chat_id': self.chat_id, 'text': text, 'parse_mode': 'Markdown'}
            if keyboard:
                payload['reply_markup'] = json.dumps(keyboard)
            requests.post(url, data=payload, timeout=10)
        except Exception as e:
            print(f"⚠️ Errore Telegram: {e}")

    def check_incoming_commands(self):
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/getUpdates?offset={self.update_offset + 1}&timeout=1"
            response = requests.get(url, timeout=5).json()
            
            if "result" in response:
                for update in response["result"]:
                    self.update_offset = update["update_id"]
                    self.save_telegram_offset()
                    
                    if "callback_query" in update:
                        callback = update["callback_query"]
                        data = callback["data"]
                        self.handle_button_click(data)
                        requests.post(f"https://api.telegram.org/bot{self.telegram_token}/answerCallbackQuery", 
                                      data={'callback_query_id': callback['id'], 'text': 'Ricevuto!'})
        except Exception as e:
            print(f"Errore check_incoming_commands: {e}")

    def handle_button_click(self, data):
        try:
            action, symbol = data.split("_")
            if symbol not in self.active_trades: return

            entry = self.active_trades[symbol]['entry']
            
            if action == "TP1":
                self.save_to_history(symbol, "TP1_HIT_MANUAL", 0, self.active_trades[symbol].get('db_id'))
                self.send_telegram(f"✅ **{symbol}**: TP1 Registrato. Stop a BE.")
            elif action == "TP2":
                self.save_to_history(symbol, "TP2_HIT_MANUAL", 0, self.active_trades[symbol].get('db_id'))
                self.send_telegram(f"✅ **{symbol}**: TP2 Registrato.")
            elif action == "TP3":
                self.save_to_history(symbol, "TP3_HIT_MANUAL", 0, self.active_trades[symbol].get('db_id'))
                self.send_telegram(f"✅ **{symbol}**: TP3 Registrato. Valuta chiusura.")
            elif action == "ACTIVE":
                self.send_telegram(f"🟢 **{symbol}**: Modalità 'Moonbag' attivata.")
            elif action == "CLOSE":
                self.save_to_history(symbol, "MANUAL_CLOSE", 0, self.active_trades[symbol].get('db_id'))
                # Aggiorna DB: imposta stato closed e result
                if 'db_id' in self.active_trades[symbol]:
                    conn = sqlite3.connect(self.db_file)
                    c = conn.cursor()
                    c.execute("UPDATE trades SET status='closed', closed_at=?, result='MANUAL_CLOSE' WHERE id=?", 
                              (datetime.now().isoformat(), self.active_trades[symbol]['db_id']))
                    conn.commit()
                    conn.close()
                del self.active_trades[symbol]
                self.save_active_trades()
                self.send_telegram(f"🏁 **{symbol}**: Posizione CHIUSA Manualmente. Slot LIBERATO.")
        except Exception as e:
            print(f"Errore bottone: {e}")

    def generate_keyboard(self, symbol):
        return {
            "inline_keyboard": [
                [{"text": "🎯 TP1 PRESO", "callback_data": f"TP1_{symbol}"}, {"text": "🎯 TP2 PRESO", "callback_data": f"TP2_{symbol}"}],
                [{"text": "🎯 TP3 PRESO", "callback_data": f"TP3_{symbol}"}, {"text": "🟢 ANCORA ATTIVO", "callback_data": f"ACTIVE_{symbol}"}],
                [{"text": "🏁 CHIUSURA MANUALE", "callback_data": f"CLOSE_{symbol}"}]
            ]
        }

    # --- MONITORAGGIO LIVE POTENZIATO (TP2, TP3, BE dinamico) ---
    def send_update_msg(self, symbol, update_type, price, trade_id=None):
        if update_type == "SL_HIT":
             msg = f"🚫 **STOP LOSS: {symbol}**\nPrezzo: `{price:.2f}`\n🗑️ Slot Liberato."
        elif update_type == "BE_HIT":
             msg = f"🛡️ **BREAK-EVEN: {symbol}**\nUscita a pareggio.\n♻️ Slot Liberato."
        elif update_type == "TP1_HIT":
             msg = f"🔔 **{symbol}**: TP1 Raggiunto (`{price:.2f}`). Sposta SL a Entry."
        elif update_type == "TP2_HIT":
             msg = f"💰 **{symbol}**: TP2 Raggiunto (`{price:.2f}`). Ottimo! Valuta trailing."
        elif update_type == "TP3_HIT":
             msg = f"🏆 **{symbol}**: TP3 Raggiunto (`{price:.2f}`). Runner in corso..."
        elif update_type == "MISSED":
             msg = f"🏃‍♂️💨 **SEGNALE SCADUTO: {symbol}**\nIl prezzo ha raggiunto TP1 senza ritracciare all'Entry Limit.\n🗑️ Trade Annullato."
        elif update_type == "FILLED":
             msg = f"⚡ **ORDINE ESEGUITO: {symbol}**\nPrezzo ritracciato all'Entry Limit (`{price:.2f}`).\n🟢 Posizione ora ATTIVA!"
        else: return
        self.send_telegram(msg)
        if trade_id:
            self.log_trade_event(trade_id, update_type, price)

    def monitor_active_trades(self):
        if not self.active_trades: return
        to_delete = []
        for symbol in list(self.active_trades.keys()):
            trade = self.active_trades[symbol]
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
            except Exception as e:
                print(f"Errore fetch ticker {symbol}: {e}")
                continue

            entry = trade['entry']
            sl_price = trade['levels'][0]
            tp1 = trade['levels'][1]
            tp2 = trade['levels'][2]
            tp3 = trade['levels'][3]
            side = trade['side']
            alerts = trade.get('alerts_sent', [])
            status = trade.get('status', 'PENDING')
            db_id = trade.get('db_id')  # ID nel database

            # 1. LOGICA PRE-ENTRY (In attesa del Limit)
            if status == 'PENDING':
                if side == "BUY":
                    if current_price <= entry:
                        trade['status'] = 'ACTIVE'
                        self.send_update_msg(symbol, "FILLED", entry, db_id)
                        # Aggiorna DB: imposta status active
                        if db_id:
                            conn = sqlite3.connect(self.db_file)
                            c = conn.cursor()
                            c.execute("UPDATE trades SET status='active', updated_at=? WHERE id=?", 
                                      (datetime.now().isoformat(), db_id))
                            conn.commit()
                            conn.close()
                    elif current_price >= tp1:
                        self.send_update_msg(symbol, "MISSED", current_price, db_id)
                        self.save_to_history(symbol, "MISSED", current_price, db_id)
                        # Aggiorna DB: chiudi come missed
                        if db_id:
                            conn = sqlite3.connect(self.db_file)
                            c = conn.cursor()
                            c.execute("UPDATE trades SET status='closed', result='MISSED', closed_at=? WHERE id=?", 
                                      (datetime.now().isoformat(), db_id))
                            conn.commit()
                            conn.close()
                        to_delete.append(symbol)
                        
                        
                elif side == "SELL":
                    if current_price >= entry:
                        trade['status'] = 'ACTIVE'
                        self.send_update_msg(symbol, "FILLED", entry, db_id)
                        if db_id:
                            conn = sqlite3.connect(self.db_file)
                            c = conn.cursor()
                            c.execute("UPDATE trades SET status='active', updated_at=? WHERE id=?", 
                                      (datetime.now().isoformat(), db_id))
                            conn.commit()
                            conn.close()
                    elif current_price <= tp1:
                        self.send_update_msg(symbol, "MISSED", current_price, db_id)
                        self.save_to_history(symbol, "MISSED", current_price, db_id)
                        if db_id:
                            conn = sqlite3.connect(self.db_file)
                            c = conn.cursor()
                            c.execute("UPDATE trades SET status='closed', result='MISSED', closed_at=? WHERE id=?", 
                                      (datetime.now().isoformat(), db_id))
                            conn.commit()
                            conn.close()
                        to_delete.append(symbol)
                        

            # 2. LOGICA POST-ENTRY (Trade Attivo) - Ora gestisce TP2 e TP3
            elif status == 'ACTIVE':
                if side == "BUY":
                    # Stop Loss
                    if current_price <= sl_price:
                        self.send_update_msg(symbol, "SL_HIT", current_price, db_id)
                        if db_id:
                            conn = sqlite3.connect(self.db_file)
                            c = conn.cursor()
                            c.execute("UPDATE trades SET status='closed', result='SL', exit_price=?, closed_at=? WHERE id=?", 
                                      (current_price, datetime.now().isoformat(), db_id))
                            conn.commit()
                            conn.close()
                        to_delete.append(symbol)
                        # Safety switch: incrementa perdite consecutive
                        self.consecutive_losses += 1
                        print(f"⚠️ Perdita consecutiva #{self.consecutive_losses} per {symbol}")
                    
                    # TP1
                    elif current_price >= tp1 and "TP1" not in alerts:
                        self.send_update_msg(symbol, "TP1_HIT", current_price, db_id)
                        trade['alerts_sent'].append("TP1")
                        # Sposta SL a entry (Break Even)
                        # Nota: non modifichiamo sl_price qui, ma registriamo che BE è attivo.
                        # Il BE sarà controllato dopo TP1
                    
                    # TP2
                    elif current_price >= tp2 and "TP2" not in alerts:
                        self.send_update_msg(symbol, "TP2_HIT", current_price, db_id)
                        trade['alerts_sent'].append("TP2")
                        # Opzionale: spostare SL a tp1? Lasciamo decidere all'utente via bottoni
                    
                    # TP3
                    elif current_price >= tp3 and "TP3" not in alerts:
                        self.send_update_msg(symbol, "TP3_HIT", current_price, db_id)
                        trade['alerts_sent'].append("TP3")
                    
                    # Break Even dopo TP1
                    elif "TP1" in alerts and current_price <= entry:
                        self.send_update_msg(symbol, "BE_HIT", current_price, db_id)
                        if db_id:
                            conn = sqlite3.connect(self.db_file)
                            c = conn.cursor()
                            c.execute("UPDATE trades SET status='closed', result='BE', exit_price=?, closed_at=? WHERE id=?", 
                                      (current_price, datetime.now().isoformat(), db_id))
                            conn.commit()
                            conn.close()
                        to_delete.append(symbol)
                        self.consecutive_losses = 0
                
                elif side == "SELL":
                    if current_price >= sl_price:
                        self.send_update_msg(symbol, "SL_HIT", current_price, db_id)
                        if db_id:
                            conn = sqlite3.connect(self.db_file)
                            c = conn.cursor()
                            c.execute("UPDATE trades SET status='closed', result='SL', exit_price=?, closed_at=? WHERE id=?", 
                                      (current_price, datetime.now().isoformat(), db_id))
                            conn.commit()
                            conn.close()
                        to_delete.append(symbol)
                        # Safety switch: incrementa perdite consecutive
                        self.consecutive_losses += 1
                        print(f"⚠️ Perdita consecutiva #{self.consecutive_losses} per {symbol}")
                    
                    elif current_price <= tp1 and "TP1" not in alerts:
                        self.send_update_msg(symbol, "TP1_HIT", current_price, db_id)
                        trade['alerts_sent'].append("TP1")
                    
                    elif current_price <= tp2 and "TP2" not in alerts:
                        self.send_update_msg(symbol, "TP2_HIT", current_price, db_id)
                        trade['alerts_sent'].append("TP2")
                    
                    elif current_price <= tp3 and "TP3" not in alerts:
                        self.send_update_msg(symbol, "TP3_HIT", current_price, db_id)
                        trade['alerts_sent'].append("TP3")
                    
                    elif "TP1" in alerts and current_price >= entry:
                        self.send_update_msg(symbol, "BE_HIT", current_price, db_id)
                        if db_id:
                            conn = sqlite3.connect(self.db_file)
                            c = conn.cursor()
                            c.execute("UPDATE trades SET status='closed', result='BE', exit_price=?, closed_at=? WHERE id=?", 
                                      (current_price, datetime.now().isoformat(), db_id))
                            conn.commit()
                            conn.close()
                        to_delete.append(symbol)
                        self.consecutive_losses = 0
            
            if symbol not in to_delete:
                self.active_trades[symbol] = trade

        for sym in to_delete:
            if sym in self.active_trades:
                del self.active_trades[sym]
        self.update_safety_mode()
        self.save_active_trades()

    def get_data(self, symbol, tf, limit=250):
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=tf, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
            return df
        except Exception as e:
            print(f"Errore get_data {symbol} {tf}: {e}")
            return None

    # --- DATI DOMINANCE BTC ---
    def get_btc_strength(self):
        try:
            ticker = self.exchange.fetch_ticker('BTC/USDT')
            return ticker.get('percentage', 0.0)
        except Exception as e:
            print(f"Errore get_btc_strength: {e}")
            return 0.0

    # --- Update market regime ---

    def update_market_regime(self, symbol='BTC/USDT', timeframe='1h'):
        """
        Calcola il regime di mercato basato su ADX, ATR e direzione.
        Aggiorna self.regime con un dizionario contenente:
        - type: 'strong_trend', 'ranging', 'transition'
        - direction: 'bull' o 'bear'
        - volatility: 'high' o 'low'
        - adx: valore ADX
        - atr_percent: ATR% (ATR/close*100)
        """
        try:
            df = self.get_data(symbol, timeframe, limit=100)
            if df is None or len(df) < 50:
                print("⚠️ Dati insufficienti per calcolare il regime")
                return
            # Calcola indicatori con pandas_ta
            df['adx'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']
            df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
            df['ema50'] = ta.ema(df['close'], length=50)

            # Prendi l'ultimo valore
            last = df.iloc[-1]
            adx = last['adx']
            atr_percent = (last['atr'] / last['close']) * 100
            direction = 'bull' if last['close'] > last['ema50'] else 'bear'

            # Classificazione regime
            if adx >= 25:
                regime_type = 'strong_trend'
            elif adx <= 20:
                regime_type = 'ranging'
            else:
                regime_type = 'transition'

            # Volatilità (soglia 2% come esempio, puoi regolarla)
            volatility = 'high' if atr_percent > 2.0 else 'low'

            self.regime = {
            'type': regime_type,
            'direction': direction,
            'volatility': volatility,
            'adx': adx,
            'atr_percent': atr_percent
        }
        except Exception as e:
            print(f"Errore in update_market_regime: {e}")
            self.log_error(f"update_market_regime: {e}")

    # ==========================================
    # 📊 AGGIORNA MOLTIPLICATORE VOLUME IN BASE AL REGIME
    # ==========================================
    def update_vol_multiplier(self):
        """Aggiorna il moltiplicatore volume in base al regime di mercato e al giorno (weekend)."""
        if self.regime is None:
            self.vol_multiplier = 1.5
            return
        
        # Logica base: volatilità
        if self.regime['volatility'] == 'low':
            self.vol_multiplier = 1.2   # bassa volatilità → accetta volumi più bassi
        elif self.regime['volatility'] == 'high':
            self.vol_multiplier = 1.8   # alta volatilità → vuoi volume più alto
        else:
            self.vol_multiplier = 1.5   # normale

        # Se è weekend, abbassa ulteriormente il moltiplicatore (mercato meno liquido)
        oggi = datetime.now().weekday()  # 0 = lunedì, 6 = domenica
        if oggi >= 5:  # sabato (5) o domenica (6)
            self.vol_multiplier = max(1.0, self.vol_multiplier - 0.3)  # riduci ma non sotto 1.0

        # --- regolazione in base alla modalità ---
        if self.mode == 'conservative':
            self.vol_multiplier += 0.3   # richiedi volume più alto
        elif self.mode == 'aggressive':
            self.vol_multiplier = max(1.0, self.vol_multiplier - 0.2)   # accetta VOLUME + basso

        # In base al tipo di regime
        if self.regime['type'] == 'ranging':
            self.vol_multiplier = min(self.vol_multiplier, 1.3)  # in range vuoi volume comunque non troppo alto

    # ==========================================
    # 🛡️ SAFETY SWITCH – GESTIONE MODALITÀ E COOLDOWN
    # ==========================================
    def update_safety_mode(self):
        """Aggiorna la modalità in base alle perdite consecutive e al cooldown."""
        now = datetime.now()

        # 1. Controlla se il cooldown è scaduto
        if self.cooldown_until and now >= self.cooldown_until:
            self.mode = 'normal'
            self.cooldown_until = None
            self.consecutive_losses = 0
            self.send_telegram("🟢 **Modalità NORMAL ripristinata** (cooldown terminato).")
            return  # Esci, perché abbiamo già cambiato modalità
        
        # 2. Se siamo già in conservative a causa del cooldown (non ancora scaduto), non fare nulla
        if self.mode == 'conservative':
            return

        # 3. Se siamo in aggressive e abbiamo 2 o più perdite consecutive, passa a conservative
        if self.mode == 'aggressive' and self.consecutive_losses >= 2:
            self.mode = 'conservative'
            self.cooldown_until = now + timedelta(hours=4)
            self.send_telegram("🔴 **MODALITÀ CONSERVATIVE ATTIVATA** per 4 ore (2 perdite consecutive).")
            return
        # Altrimenti, nessun cambiamento (la modalità rimane quella corrente)


    # ==========================================
    # 🔥 HTF CHECKER POTENZIATO (USA STRUTTURA DI MERCATO)
    # ==========================================
    def check_htf_confluence(self, symbol, side, required_score=2):
        """
        Analizza 30m, 1h, 2h usando struttura (massimi/minimi) ed EMA.
        Restituisce True se almeno 'required_score' timeframe su 3 sono allineati.
        """
        try:
            timeframes = ['30m', '1h', '2h']
            confluence_score = 0
            

            for tf in timeframes:
                df = self.get_data(symbol, tf, limit=100)
                if df is None or df.empty: return False
                
                # Calcola EMA 20 e 50
                ema20 = ta.ema(df['close'], length=20).iloc[-1]
                ema50 = ta.ema(df['close'], length=50).iloc[-1]
                close = df['close'].iloc[-1]
                
                # Identifica trend: se EMA20 > EMA50 e close > EMA20 => uptrend
                uptrend = (ema20 > ema50) and (close > ema20)
                downtrend = (ema20 < ema50) and (close < ema20)
                
                # Struttura: ultimo massimo/minimo significativo
                high_last = df['high'].iloc[-5:].max()
                low_last = df['low'].iloc[-5:].min()
                
                if side == "BUY":
                    if uptrend and low_last > ema50 * 0.98:  # mantiene struttura rialzista
                        confluence_score += 1
                elif side == "SELL":
                    if downtrend and high_last < ema50 * 1.02:
                        confluence_score += 1
            
            return confluence_score >= required_score
        except Exception as e:
            print(f"Errore HTF Check {symbol}: {e}")
            return False
    
    # ==========================================
    # 🏦 FILTRO MONEY FLOW POTENZIATO (OI + Funding)
    # ==========================================
    def check_money_flow(self, symbol, side):
        try:
            funding = self.exchange.fetch_funding_rate(symbol)
            f_rate = funding.get('fundingRate', 0)
            
            # Soglia dinamica in base alla volatilità (potremmo usare ATR)
            threshold = 0.03 / 100 

            if side == "BUY" and f_rate > threshold:
                return False, f"Funding Too High ({f_rate*100:.3f}%)"
            if side == "SELL" and f_rate < -threshold:
                return False, f"Funding Too Low ({f_rate*100:.3f}%)"
            
            # Open Interest (se disponibile)
            try:
                oi = self.exchange.fetch_open_interest(symbol)
                oi_value = oi.get('openInterest', 0)
                # Potremmo confrontare con media mobile, ma per ora passiamo
            except:
                pass
            
            return True, "Flow OK"
        except Exception as e:
            print(f"Errore check_money_flow {symbol}: {e}")
            return True, "Flow N/A"

    # ==========================================
    # 📊 MARKET PROFILE AVANZATO (CON VWAP)
    # ==========================================
    def get_market_profile_data(self, df):
        try:
            # Utilizziamo 150 candele
            df_context = df.tail(150).copy()
            price_min = df_context['low'].min()
            price_max = df_context['high'].max()
            
            bins = 70
            price_bins = np.linspace(price_min, price_max, bins + 1)
            v_profile = np.zeros(bins)

            for _, row in df_context.iterrows():
                h, l, v = row['high'], row['low'], row['volume']
                mask = (price_bins[:-1] <= h) & (price_bins[1:] >= l)
                num_bins_covered = np.sum(mask)
                
                if num_bins_covered > 0:
                    v_profile[mask] += v / num_bins_covered
                else:
                    bin_idx = np.digitize((h + l) / 2, price_bins) - 1
                    if 0 <= bin_idx < bins:
                        v_profile[bin_idx] += v

            max_vol_idx = np.argmax(v_profile)
            poc_price = (price_bins[max_vol_idx] + price_bins[max_vol_idx + 1]) / 2

            total_volume = np.sum(v_profile)
            va_volume = total_volume * 0.70
            
            current_va_vol = v_profile[max_vol_idx]
            low_idx = max_vol_idx
            high_idx = max_vol_idx
            
            while current_va_vol < va_volume:
                prev_low_vol = v_profile[low_idx - 1] if low_idx > 0 else 0
                next_high_vol = v_profile[high_idx + 1] if high_idx < bins - 1 else 0
                
                if prev_low_vol >= next_high_vol and low_idx > 0:
                    low_idx -= 1
                    current_va_vol += prev_low_vol
                elif high_idx < bins - 1:
                    high_idx += 1
                    current_va_vol += next_high_vol
                else:
                    break

            val_price = price_bins[low_idx]
            vah_price = price_bins[high_idx + 1]

            # Calcola VWAP
            df_context['tp'] = (df_context['high'] + df_context['low'] + df_context['close']) / 3
            vwap = (df_context['tp'] * df_context['volume']).sum() / df_context['volume'].sum()

            return {
                'poc': poc_price,
                'vah': vah_price,
                'val': val_price,
                'vwap': vwap
            }
        except Exception as e:
            print(f"Errore Market Profile: {e}")
            return None

    # --- MODULO SMC AVANZATO (Breaker, Mitigazione, Order Block, FVG) ---
    def get_smc_analysis(self, df):
        try:
            atr = ta.atr(df['high'], df['low'], df['close'], length=14).iloc[-1]
            
            lookback = 20  # aumentato per migliori sweep
            prev_highs = df['high'].iloc[-lookback:-2].max()
            prev_lows = df['low'].iloc[-lookback:-2].min()
            
            c1 = df.iloc[-2] 
            
            sweep_high = (c1['high'] > prev_highs) and (c1['close'] < prev_highs)
            sweep_low = (c1['low'] < prev_lows) and (c1['close'] > prev_lows)
            
            # FVG (Fair Value Gap) più robusto
            c_post = df.iloc[-2] 
            c_mid = df.iloc[-3]  
            c_pre = df.iloc[-4]  
            
            min_gap = atr * 0.05
            min_body = atr * 0.3 
            mid_body = abs(c_mid['close'] - c_mid['open'])
            
            fvg_bull = False
            fvg_bear = False
            
            if c_post['low'] > (c_pre['high'] + min_gap):
                if mid_body > min_body and c_mid['close'] > c_mid['open']: 
                    fvg_bull = True
            
            if c_post['high'] < (c_pre['low'] - min_gap):
                if mid_body > min_body and c_mid['close'] < c_mid['open']: 
                    fvg_bear = True
                    
            # Order Block (OB) - la candela prima del FVG
            ob_bull = fvg_bull and (c_pre['close'] < c_pre['open'])  # bearish candle prima di FVG rialzista
            ob_bear = fvg_bear and (c_pre['close'] > c_pre['open'])  # bullish candle prima di FVG ribassista

            # Breaker Block (inversione di struttura)
            # Rileviamo un possibile breaker: ultimo massimo/minimo rotto con forza
            # Semplificato: dopo un sweep, se c'è un FVG opposto
            breaker_bull = sweep_low and fvg_bull
            breaker_bear = sweep_high and fvg_bear

            # Mitigazione: prezzo torna nell'OB
            # Non implementata qui, ma potremmo segnalare se il prezzo è vicino all'OB

            # Entry basata su FVG o OB
            entry_bull = (c_post['low'] + c_pre['high']) / 2 if fvg_bull else c_pre['high']
            entry_bear = (c_post['high'] + c_pre['low']) / 2 if fvg_bear else c_pre['low']

            return {
                'sweep_low': sweep_low,
                'sweep_high': sweep_high,
                'fvg_bull': fvg_bull,
                'fvg_bear': fvg_bear,
                'ob_bull': ob_bull,
                'ob_bear': ob_bear,
                'breaker_bull': breaker_bull,
                'breaker_bear': breaker_bear,
                'entry_bull': entry_bull,
                'entry_bear': entry_bear,
                'atr': atr
            }
        except Exception as e:
            print(f"Errore get_smc_analysis: {e}")
            return {'sweep_low': False, 'sweep_high': False, 'fvg_bull': False, 'fvg_bear': False, 'ob_bull': False, 'ob_bear': False, 'breaker_bull': False, 'breaker_bear': False, 'entry_bull': 0, 'entry_bear': 0, 'atr': 0}

    # --- FILTRI ACCESSORI ---
    def check_volume_confirmation(self, df, side):
        vol = df['volume'].iloc[-2]
        avg_vol = df['volume'].tail(20).mean()
        if vol < avg_vol * self.vol_multiplier:  # Usa il moltiplicatore dinamico
            return False 
        return True

    def calculate_levels(self, price, atr, side):
        if side == "WAIT":
            return None, None, None, None
            
        mult = 1 if side == "BUY" else -1
        sl = price - (2.0 * atr * mult)
        tp1 = price + (1.5 * atr * mult)
        tp2 = price + (2.5 * atr * mult)
        tp3 = price + (4.0 * atr * mult)
        return sl, tp1, tp2, tp3

    # ==========================================
    # 🧠 CUORE DECISIONALE V9
    # ==========================================
    def process_market(self, symbol):
        self.update_safety_mode()  #SAFETY SWITCH
        if symbol in self.active_trades:
            return {'symbol': symbol, 'status': 'LOCKED', 'smc_context': 'IN_TRADE'}

        if symbol != 'BTC/USDT':
            btc_strength = self.get_btc_strength()
            if btc_strength > self.btc_dom_threshold:
                return {
                    'symbol': symbol,
                    'status': 'SKIP_DOMINANCE',
                    'side': 'WAIT',
                    'smc_context': f'BTC DOMINANCE HIGH (+{btc_strength:.2f}%)'
                }

        df = self.get_data(symbol, '15m', limit=1000) 
        if df is None or len(df) < 500: return None

        df['EMA_20'] = ta.ema(df['close'], length=20)
        df['RSI'] = ta.rsi(df['close'], length=14)
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
        df['Target_Pct'] = df['close'].shift(-1) / df['close'] - 1 
        
        df_clean = df.dropna().copy()
        feats = ['RSI', 'volume', 'ATR'] 
        
        current_time = datetime.now()
        if not hasattr(self, 'last_train_time'): 
            self.last_train_time = {}
        
        # Intervallo di ri-addestramento: 6 ore
        retrain_interval = timedelta(hours=6)

        # Gestione modello per simbolo
        if symbol not in self.models:
            self.models[symbol] = xgb.XGBRegressor(n_estimators=150, max_depth=5, learning_rate=0.07, random_state=42, n_jobs=-1)
            self.last_train_time[symbol] = datetime.min  # Forza il primo addestramento 
        last_train = self.last_train_time.get(symbol, datetime.min)
        if (current_time - last_train) > retrain_interval:
            self.models[symbol].fit(df_clean[feats], df_clean['Target_Pct'])
            self.last_train_time[symbol] = current_time
            print(f"🧠 Modello {symbol} aggiornato alle {current_time.strftime('%H:%M:%S')}")

        last_feats = df_clean[feats].iloc[-1:]
        pred_change = self.models[symbol].predict(last_feats)[0]
        
        ia_diff = pred_change 
        curr_price = df['close'].iloc[-1]

        recent = df.tail(5)
        buy_v = recent[recent['close'] > recent['open']]['volume'].sum()
        sell_v = recent[recent['close'] < recent['open']]['volume'].sum()
        pressione_vol = "BULL" if buy_v > (sell_v * 1.2) else "BEAR" if sell_v > (buy_v * 1.2) else "NEUTRAL"

        confidenza = min(100, abs(ia_diff) * 100000)
        
        # Determina soglia in base al regime di mercato
        base_threshold = 65.0
        if self.regime is not None:
            regime_type = self.regime['type']
            if regime_type == 'strong_trend':
                threshold = base_threshold - 5   # 60% in trend forte
            elif regime_type == 'ranging':
                threshold = base_threshold + 10  # 75% in range
            else:  # 'transition'
                threshold = base_threshold       # 65% in transizione
        else:
            threshold = base_threshold           # default se regime non disponibile

        # Aggiustamento in base alla modalità di sicurezza
        if self.mode == 'conservative':
            threshold += 5      # più cauto: richiede confidenza maggiore
        elif self.mode == 'aggressive':
            threshold -= 5      # più aggressivo: accetta confidenza minore

        smc = self.get_smc_analysis(df)
        profile = self.get_market_profile_data(df)
        
        side = "WAIT"
        smc_text = "Scanning..."
        htf_info = "---"
        limit_entry_price = curr_price
        poc_ok = True

        # --- NUOVA PARTE: calcolo htf_required ---
        if self.regime and self.regime['type'] == 'strong_trend':
            htf_required = 1
        else:
            htf_required = 2
        
        # Aggiustamento in base alla modalità
        if self.mode == 'conservative':
            htf_required = min(3, htf_required + 1)   # richiedi un timeframe in più
        elif self.mode == 'aggressive':
            htf_required = max(1, htf_required - 1)   # richiedi un timeframe in meno
        # ----------------------------------------- 

        if ia_diff > 0.0008 and confidenza >= threshold: 
            # Setup rialzista: sweep low, FVG bull, OB bull, breaker bull
            if smc['sweep_low'] or smc['fvg_bull'] or smc['ob_bull'] or smc['breaker_bull']:
                if self.check_volume_confirmation(df, "BUY") and pressione_vol != "BEAR":
                    if self.check_htf_confluence(symbol, "BUY", required_score=htf_required):
                        flow_ok, flow_msg = self.check_money_flow(symbol, "BUY")
                        
                        if profile:
                            if curr_price < profile['poc']:
                                poc_ok = False
                                flow_msg = "Sotto POC (Debolezza)"
                        
                        if flow_ok and poc_ok:
                            side = "BUY"
                            smc_text = f"INSTITUTIONAL BUY | {flow_msg}"
                            limit_entry_price = smc['entry_bull']
                            htf_info = "✅ ALL CLEAR"
                        else:
                            smc_text = f"DENIED: {flow_msg if not flow_ok or not poc_ok else ''}"
                            htf_info = "❌ INST. REJECT"
                    else:
                        smc_text = "HTF DENIED (Trend Bearish)"
                        htf_info = "❌ MISMATCH"

        elif ia_diff < -0.0008 and confidenza >= threshold: 
            if smc['sweep_high'] or smc['fvg_bear'] or smc['ob_bear'] or smc['breaker_bear']:
                if self.check_volume_confirmation(df, "SELL") and pressione_vol != "BULL":
                    if self.check_htf_confluence(symbol, "SELL", required_score=htf_required):
                        flow_ok, flow_msg = self.check_money_flow(symbol, "SELL")
                        
                        if profile:
                            if curr_price > profile['poc']:
                                poc_ok = False
                                flow_msg = "Sopra POC (Forza Retail)"

                        if flow_ok and poc_ok:
                            side = "SELL"
                            smc_text = f"INSTITUTIONAL SELL | {flow_msg}"
                            limit_entry_price = smc['entry_bear']
                            htf_info = "✅ ALL CLEAR"
                        else:
                            smc_text = f"DENIED: {flow_msg if not flow_ok or not poc_ok else ''}"
                            htf_info = "❌ INST. REJECT"
                    else:
                        smc_text = "HTF DENIED (Trend Bullish)"
                        htf_info = "❌ MISMATCH"
        sl, tp1, tp2, tp3 = self.calculate_levels(limit_entry_price, smc['atr'], side)

        return {
            'symbol': symbol,
            'side': side,
            'price': limit_entry_price, 
            'current_price': curr_price,
            'conf': confidenza, 
            'smc_context': smc_text,
            'status': 'ACTIVE',
            'levels': (sl, tp1, tp2, tp3),
            'smc_data': smc,
            'htf_info': htf_info
        }

    def run(self):
        while True:
            try:
                os.system('cls' if os.name == 'nt' else 'clear')
                print(f"👹 SNIPER PROTOCOL V9 (SMC EVOLVED) | {datetime.now().strftime('%H:%M:%S')}")
                print(f"Parametri: 15m SMC + HTF Avanzato (30m, 1h, 2h) | BTC Threshold: +{self.btc_dom_threshold}%")
                print(f"Filtro IA Attivo: Confidenza minima adattiva (base 65%, varia con regime)")
                self.update_market_regime()
                self.update_vol_multiplier()
                if self.regime:
                    print(f"📊 Regime: {self.regime['type']} {self.regime['direction']} | Vol: {self.regime['volatility']} | ADX: {self.regime['adx']:.1f} | VolMult: {self.vol_multiplier}") 
                print("-" * 115)
                
                print(f"{'ASSET':<8} | {'IA BIAS':<10} | {'SMC CONTEXT (LIQUIDITY)':<27} | {'HTF':<10} | {'ACTION'}")
                print("-" * 115)

                for symbol in self.watchlist:
                    m = self.process_market(symbol)
                    if not m: continue
                    
                    if m['status'] == 'LOCKED':
                         print(f"{symbol:<8} | {'---':<10} | {'TRADE IN CORSO':<27} | {'---':<10} | {'HOLD'}")
                         continue
                    
                    if m.get('status') == 'SKIP_DOMINANCE':
                         print(f"{symbol:<8} | {'---':<10} | {'SKIP - BTC DOMINANCE HIGH':<27} | {'---':<10} | {'PAUSED'}")
                         continue

                    smc_status = m['smc_context']
                    htf_status = m.get('htf_info', '---')
                    
                    if m['conf'] < 65.0:
                        bias = f"⚪ LOW ({m['conf']:.0f}%)"
                    else:
                        bias = "🟢 BULL" if m['side'] == "BUY" else ("🔴 BEAR" if m['side'] == "SELL" else "⚪ NEUT")
                    
                    if m['side'] == "WAIT":
                        if "HTF DENIED" in smc_status:
                             pass
                        elif m['smc_data']['sweep_low']: smc_status = "👀 SWEEP LOW DETECTED"
                        elif m['smc_data']['sweep_high']: smc_status = "👀 SWEEP HIGH DETECTED"
                        elif m['smc_data']['fvg_bull']: smc_status = "⚠️ FVG BULL (No Trigger)"
                        elif m['smc_data']['fvg_bear']: smc_status = "⚠️ FVG BEAR (No Trigger)"
                        elif m['smc_data']['breaker_bull']: smc_status = "⚡ BREAKER BULL (No Trigger)"
                        elif m['smc_data']['breaker_bear']: smc_status = "⚡ BREAKER BEAR (No Trigger)"
                        else: smc_status = "Balanced / Ranging"
                        
                    print(f"{symbol:<8} | {bias:<10} | {smc_status:<27} | {htf_status:<10} | {m['side']}")

                    if m['side'] != "WAIT":
                        self.trigger_agent_signal(m)

                for _ in range(20):
                    self.check_incoming_commands()
                    self.monitor_active_trades()
                    time.sleep(15)

                print("-" * 115)
                print("Analisi completata. Monitoraggio attivo per i prossimi 5m...") 
            
            except Exception as e:
                print(f"⚠️ ERRORE CRITICO NEL CICLO: {e}")
                self.log_error(f"CRITICAL: {e}")
                time.sleep(30)

    def trigger_agent_signal(self, m):
        sig_id = f"{m['symbol']}_{m['side']}_{datetime.now().hour}_{datetime.now().minute}"
        if sig_id in self.signal_history: return
        self.signal_history.append(sig_id)

        sl, tp1, tp2, tp3 = m['levels']
        emoji = "🚀" if m['side'] == "BUY" else "🩸"
        
        # Inserisci trade nel database
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute('''INSERT INTO trades 
                     (symbol, side, entry_price, status, sl, tp1, tp2, tp3, created_at, updated_at)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                  (m['symbol'], m['side'], m['price'], 'pending', sl, tp1, tp2, tp3,
                   datetime.now().isoformat(), datetime.now().isoformat()))
        trade_id = c.lastrowid
        conn.commit()
        conn.close()
        
        msg = (
            f"👹 **SNIPER PROTOCOL V9 - ISTITUZIONALE**\n"
            f"📍 **Asset:** `{m['symbol']}`\n"
            f"⚡ **Azione:** *{m['side']}* {emoji}\n\n"
            
            f"📊 **SMC CONTEXT:**\n"
            f"🔹 Setup: `{m['smc_context']}`\n"
            f"🧭 Trend HTF: `✅ CONFIRMED (30m/1h/2h)`\n"
            f"🧱 Zone: `Validated Order Block`\n\n"
            
            f"🧠 **IA BIAS:** Confidenza {m['conf']:.0f}%\n\n"
            
            f"💰 **LIVELLI OPERATIVI:**\n"
            f"📥 **Entry (LIMIT):** `{m['price']:.2f}`\n"
            f"⏳ *Il segnale è valido SOLO se il prezzo ritraccia a questo livello.*\n"
            f"🛡️ **STOP LOSS:** `{sl:.2f}`\n\n"
            
            f"🎯 **TAKE PROFIT:**\n"
            f"1️⃣ `{tp1:.2f}` (Safe)\n"
            f"2️⃣ `{tp2:.2f}` (Target)\n"
            f"3️⃣ `{tp3:.2f}` (Runner)\n"
        )
        
        self.active_trades[m['symbol']] = {
            'entry': m['price'], 'side': m['side'], 'levels': m['levels'],
            'start_time': str(datetime.now()), 'alerts_sent': [], 'status': 'PENDING',
            'db_id': trade_id
        }
        self.save_active_trades()
        self.save_to_history(m['symbol'], "PENDING_LIMIT_" + m['side'], m['price'], trade_id)
        
        keyboard = self.generate_keyboard(m['symbol'])
        self.send_telegram(msg, keyboard)

if __name__ == "__main__":
    SniperAgentV9().run()