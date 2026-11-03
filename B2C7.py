#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B轉C(參考A)

A : 客戶進貨資料(.zip,內含 .rpt;有無內層資料夾皆可)
B : 廠內完成資料(資料夾,內含 .txt,0=缺點 1=良品)
C : 轉換資料(.zip,檔名=B資料夾名稱,壓縮檔內是同名資料夾,資料夾內才是 .rpt)

輸入多行 "料號 尾碼 批號"(每行一組),例如: 7Q56-478  0935  22691670-000
  A = <A根路徑>\\料號\\COM_料號&尾碼.zip
  B = <B根路徑>\\料號\\批號
  轉換前會交叉檢查: A 壓縮檔內檔名去掉最後五碼(空格+四位數)必須等於輸入的批號

規則:
  1. C = A 原內容
  2. 檔頭 FLOW PASSED=AOI,ET,  ->  FLOW PASSED=AOI,ET,SOR
  3. 檔尾加上
        SOR
        (RUN 1) <執行時間>,GCSS-3100,
        X:xxx Y:yyy=N          (B 的全部 0,先 X 後 Y 排序)
  4. 只輸出 B 有的片;A 有而 B 沒有的片略過
"""
import os
import re
import sys
import json
import shutil
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

NL = "\r\n"
MACHINE = "GCSS-3100"
ENC = "latin1"  # 位元組原樣保留

# ---- 預設路徑(可在程式「設定」內修改,存於 ~/.b2c_config.json)----
DEFAULT_A_ROOT = r"\\BARCODE\e\입고 Lot\KINSUS"
DEFAULT_B_ROOT = r"\\BARCODE\e\Final\Sorter\Sorting2\KINSUS"


# --------------------------------------------------------------------------
# 核心邏輯(與介面無關,可單獨測試)
# --------------------------------------------------------------------------
def read_a_zip(a_zip):
    """回傳 {片名: 內容字串};忽略資料夾層級,只看檔名。"""
    result = {}
    with zipfile.ZipFile(a_zip) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            base = os.path.basename(info.filename)
            if base.startswith(".") or "__MACOSX" in info.filename:
                continue
            stem, ext = os.path.splitext(base)
            if ext.lower() != ".rpt":
                continue
            result[stem] = z.read(info).decode(ENC)
    return result


def parse_b(text):
    """回傳 (rows列數, cols欄數, 缺點座標集合{(x,y)})。第1行為片名,忽略。"""
    lines = text.splitlines()[1:]
    grid = []
    for ln in lines:
        s = ln.strip()
        if s and re.fullmatch(r"[01 ]+", s):
            grid.append(s.replace(" ", ""))
    cols = {len(r) for r in grid}
    if len(cols) != 1:
        raise ValueError(f"B 各列欄數不一致: {sorted(cols)}")
    defects = {(x, y)
               for y, r in enumerate(grid, 1)
               for x, ch in enumerate(r, 1) if ch == "0"}
    return len(grid), cols.pop(), defects


def build_c(a_text, b_text, now):
    """由 A 與 B 組出 C 的內容。"""
    rows, cols, defects = parse_b(b_text)

    mx = re.search(r"EACH X PIECE=(\d+)", a_text)
    my = re.search(r"EACH Y PIECE=(\d+)", a_text)
    if mx and my and (int(mx[1]) != cols or int(my[1]) != rows):
        raise ValueError(
            f"尺寸不符: A={mx[1]}x{my[1]}, B={cols}x{rows}")

    # 1) 檔頭 FLOW PASSED 補上 SOR
    def add_sor(m):
        flow = m.group(1)
        if "SOR" in flow.split(","):
            return m.group(0)
        if flow and not flow.endswith(","):
            flow += ","
        return f"FLOW PASSED={flow}SOR"
    out, n = re.subn(r"FLOW PASSED=([^\r\n]*)", add_sor, a_text, count=1)
    if n == 0:
        raise ValueError("A 找不到 FLOW PASSED 行")

    # 2) 確保結尾換行後,接上 SOR 區段
    if not out.endswith(NL):
        out += NL
    out += "SOR" + NL
    out += f"(RUN 1) {now:%Y/%m/%d %H:%M:%S},{MACHINE}," + NL
    for x, y in sorted(defects):
        out += f"X:{x:03d} Y:{y:03d}=N" + NL
    return out


def convert(a_zip, b_dir, out_dir, log=print, progress=None):
    """主流程。回傳 (C壓縮檔路徑, 成功數, 失敗清單)。"""
    b_dir = os.path.normpath(b_dir)
    folder = os.path.basename(b_dir)
    a_map = read_a_zip(a_zip)

    b_files = sorted(f for f in os.listdir(b_dir) if f.lower().endswith(".txt"))
    if not b_files:
        raise RuntimeError("B 資料夾內找不到 .txt")

    b_stems = [os.path.splitext(f)[0] for f in b_files]
    a_only = sorted(set(a_map) - set(b_stems))
    if a_only:
        log(f"略過(A有、B沒有) {len(a_only)} 片: {', '.join(a_only)}")

    os.makedirs(out_dir, exist_ok=True)
    out_zip = os.path.join(out_dir, folder + ".zip")

    ok, failed = 0, []
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zo:
        for idx, (fname, stem) in enumerate(zip(b_files, b_stems), 1):
            if progress:
                progress(idx, len(b_files))
            try:
                if stem not in a_map:
                    raise KeyError("A 內找不到對應的 .rpt")
                with open(os.path.join(b_dir, fname), "rb") as f:
                    b_text = f.read().decode(ENC)
                now = datetime.now()
                c_text = build_c(a_map[stem], b_text, now)
                # 明確指定壓縮檔內的檔案屬性,避免 writestr 預設的 0600 Unix 屬性
                zi = zipfile.ZipInfo(f"{folder}/{stem}.rpt", date_time=now.timetuple()[:6])
                zi.compress_type = zipfile.ZIP_DEFLATED
                zi.create_system = 0      # 0 = Windows (FAT/NTFS)
                zi.external_attr = 0x20   # Windows「封存」屬性,7-Zip 顯示為 A
                zo.writestr(zi, c_text.encode(ENC))
                ok += 1
            except Exception as e:  # 單片失敗不影響其他片
                failed.append((stem, str(e)))
                log(f"失敗 {stem}: {e}")

    log(f"完成: 輸出 {ok} / B 共 {len(b_files)} 片 -> {out_zip}")
    if failed:
        log(f"注意: {len(failed)} 片失敗,輸出數量與 B 不同")
    return out_zip, ok, failed


# --------------------------------------------------------------------------
# 介面
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# 本機暫存:先把網路上的 A、B 複製到本機,轉換完再刪除
# --------------------------------------------------------------------------
def stage_local(a_zip, b_dir, tmp_root, log=print, progress=None, workers=8):
    """把 A 壓縮檔與 B 的 .txt 複製到 tmp_root,回傳 (本機A, 本機B資料夾)。
    A 與 B 的多個小檔案同時複製(網路磁碟的延遲是主要瓶頸,平行複製可大幅縮短)。
    本機 B 資料夾名稱與原 B 相同(C 壓縮檔名稱取自此名稱)。"""
    folder = os.path.basename(os.path.normpath(b_dir))

    t0 = time.perf_counter()
    txts = sorted(f for f in os.listdir(b_dir) if f.lower().endswith(".txt"))
    log(f"列出 B 檔案 {len(txts)} 個,耗時 {time.perf_counter() - t0:.1f} 秒")

    local_a = os.path.join(tmp_root, "A.zip")
    local_b = os.path.join(tmp_root, folder)
    os.makedirs(local_b, exist_ok=True)

    def copy_a():
        t = time.perf_counter()
        shutil.copyfile(a_zip, local_a)
        return time.perf_counter() - t

    def copy_b(f):
        shutil.copyfile(os.path.join(b_dir, f), os.path.join(local_b, f))

    log("複製 A 壓縮檔與 B 資料到本機(同時進行)...")
    t1 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        fa = ex.submit(copy_a)
        fbs = [ex.submit(copy_b, f) for f in txts]
        try:
            for i, fut in enumerate(as_completed(fbs), 1):
                fut.result()  # 有檔案複製失敗就在這裡丟出例外
                if progress:
                    progress(i, len(txts))
            tb = time.perf_counter() - t1
            ta = fa.result()
        except Exception:
            for f in fbs:
                f.cancel()
            raise
    log(f"複製完成: A {ta:.1f} 秒, B {tb:.1f} 秒")
    return local_a, local_b


def convert_via_local(a_zip, b_dir, out_dir, log=print, progress=None):
    """複製到本機 -> 轉換 -> 刪除暫存。C 直接輸出到 out_dir。"""
    tmp_root = tempfile.mkdtemp(prefix="b2c_")
    try:
        local_a, local_b = stage_local(a_zip, b_dir, tmp_root, log, progress)
        log("開始轉換...")
        t = time.perf_counter()
        r = convert(local_a, local_b, out_dir, log, progress)
        log(f"轉換耗時 {time.perf_counter() - t:.1f} 秒")
        return r
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


# --------------------------------------------------------------------------
# 記憶上次開啟位置(A、B、C 各自獨立)
# --------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".b2c_config.json")


def load_config():
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 記憶失敗不影響主要功能


# --------------------------------------------------------------------------
# 輸入解析、路徑組合、配對檢查
# --------------------------------------------------------------------------
BAD_CHARS = set('\\/:*?"<>|&')
STEM_RE = re.compile(r"^(?P<lot>.+) (?P<seq>\d{4})$")  # 批號 + 空格 + 四位數(共最後五碼)


def get_roots(cfg):
    """目前使用的 (A根路徑, B根路徑);設定檔沒有就用預設值。"""
    return (cfg.get("root_a") or DEFAULT_A_ROOT,
            cfg.get("root_b") or DEFAULT_B_ROOT)


def parse_input(text):
    """'7Q56-478 0935 22691670-000' -> (料號, 尾碼, 批號)。空白/Tab 皆可。"""
    parts = text.split()
    if len(parts) != 3:
        raise ValueError(
            f"需要三段(料號 尾碼 批號),目前讀到 {len(parts)} 段。"
            "例: 7Q56-478  0935  22691670-000")
    for name, p in zip(("料號", "尾碼", "批號"), parts):
        if BAD_CHARS & set(p):
            raise ValueError(f"{name}含有不允許的字元: {p}")
    return tuple(parts)


def parse_lines(text):
    """多行輸入 -> 項目清單。空行忽略;格式錯誤標 bad;重複標 dup。
    每項: {no, raw, key, status(None=待查/bad/dup), msg}"""
    entries, seen = [], {}
    for no, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        e = {"no": no, "raw": raw.strip(), "key": None, "status": None, "msg": ""}
        try:
            e["key"] = parse_input(raw)
        except ValueError as ex:
            e["status"], e["msg"] = "bad", str(ex)
        else:
            if e["key"] in seen:
                e["status"] = "dup"
                e["msg"] = f"與第 {seen[e['key']]} 行重複,不會重複轉換"
            else:
                seen[e["key"]] = no
        entries.append(e)
    return entries


def build_paths(part, suffix, lot, a_root, b_root):
    a_zip = os.path.join(a_root, part, f"COM_{part}&{suffix}.zip")
    b_dir = os.path.join(b_root, part, lot)
    return a_zip, b_dir


def a_zip_stems(a_zip):
    """只讀壓縮檔目錄,回傳其中 .rpt 的檔名(不含副檔名)清單。"""
    stems = []
    with zipfile.ZipFile(a_zip) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            base = os.path.basename(info.filename)
            if base.startswith(".") or "__MACOSX" in info.filename:
                continue
            stem, ext = os.path.splitext(base)
            if ext.lower() == ".rpt":
                stems.append(stem)
    return stems


def inspect_pair(part, suffix, lot, a_root, b_root):
    """找 A、B 並做交叉檢查。有問題一律丟 ValueError(訊息可直接顯示)。"""
    a_zip, b_dir = build_paths(part, suffix, lot, a_root, b_root)

    if not os.path.isfile(a_zip):
        raise ValueError(f"找不到 A 壓縮檔: {a_zip}")
    if not os.path.isdir(b_dir):
        raise ValueError(f"找不到 B 資料夾: {b_dir}")

    try:
        a_stems = a_zip_stems(a_zip)
    except zipfile.BadZipFile:
        raise ValueError(f"A 不是有效的壓縮檔(可能還在傳輸中): {a_zip}")
    if not a_stems:
        raise ValueError(f"A 壓縮檔內沒有 .rpt: {a_zip}")

    # 交叉檢查: 去掉最後五碼後必須等於輸入的批號
    bad = [s for s in a_stems if not STEM_RE.match(s)]
    if bad:
        raise ValueError(
            f"A 內有 {len(bad)} 個檔名不符「批號 空格 四位數」格式,已停止: "
            + ", ".join(bad[:5]) + (" ..." if len(bad) > 5 else ""))
    lots = sorted({STEM_RE.match(s).group("lot") for s in a_stems})
    if lots != [lot]:
        raise ValueError(
            f"交叉檢查未通過: 你輸入的批號是 {lot},但 A 壓縮檔內的批號是 "
            f"{', '.join(lots)}。請確認尾碼與批號是否屬於同一批。")

    b_stems = sorted(os.path.splitext(f)[0] for f in os.listdir(b_dir)
                     if f.lower().endswith(".txt"))
    if not b_stems:
        raise ValueError(f"B 資料夾內找不到 .txt: {b_dir}")

    a_set, b_set = set(a_stems), set(b_stems)
    return {
        "part": part, "suffix": suffix, "lot": lot,
        "a_zip": a_zip, "b_dir": b_dir,
        "a_count": len(a_set), "b_count": len(b_set),
        "convert": len(a_set & b_set),
        "skip": sorted(a_set - b_set),      # A有、B沒有:略過
        "b_only": sorted(b_set - a_set),    # B有、A沒有:會失敗
    }


def row_message(i):
    """通過的那一行要顯示的說明。"""
    msg = (f"交叉檢查通過 | A {i['a_count']} 片 / B {i['b_count']} 片 "
           f"-> 轉換 {i['convert']} 片")
    if i["skip"]:
        msg += f" | 略過(A有B沒有) {len(i['skip'])} 片"
    if i["b_only"]:
        msg += (f" | 警告: B有A沒有 {len(i['b_only'])} 片會失敗: "
                + ", ".join(i["b_only"]))
    return msg


def summary_text(entries_status):
    """entries_status: 每行的 'ok'/'bad'/'dup' 清單 -> 摘要字串。"""
    n = len(entries_status)
    p = entries_status.count("ok")
    d = entries_status.count("dup")
    q = n - p - d
    s = f"{n} 行中 {p} 行通過,{q} 行有問題"
    if d:
        s += f"(另有 {d} 行重複已忽略)"
    return s


def run_gui():
    import queue
    import threading
    import tkinter as tk
    from concurrent.futures import ThreadPoolExecutor
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = tk.Tk()
    root.title("B轉C(參考A)")
    root.geometry("980x720")
    root.columnconfigure(1, weight=1)
    root.rowconfigure(7, weight=1)

    cfg = load_config()  # root_a / root_b / dir_c
    vo = tk.StringVar(value=cfg.get("dir_c", ""))
    vprog = tk.StringVar(value="")
    vsum = tk.StringVar(value="")
    vroots = tk.StringVar()
    state = {"busy": False, "rows": None, "cache": {}, "msgs": {}, "pending": 0,
             "test": None}
    q = queue.Queue()  # 背景執行緒 -> 介面 的訊息佇列

    def update_roots_label():
        a, b = get_roots(cfg)
        vroots.set(f"目前 A 根路徑: {a}\n目前 B 根路徑: {b}")

    # ---- 輸入區 ----
    tk.Label(root, text="料號 尾碼 批號\n(每行一組)", width=14, anchor="ne", justify="right"
             ).grid(row=0, column=0, padx=6, pady=(10, 2), sticky="ne")
    tf = tk.Frame(root)
    tf.grid(row=0, column=1, pady=(10, 2), sticky="nsew")
    tf.columnconfigure(0, weight=1)
    txt = tk.Text(tf, height=8, width=70, undo=True, wrap="none")
    sb = tk.Scrollbar(tf, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)
    txt.grid(row=0, column=0, sticky="nsew")
    sb.grid(row=0, column=1, sticky="ns")

    bf = tk.Frame(root)
    bf.grid(row=0, column=2, padx=6, pady=(10, 2), sticky="n")
    btn_q = tk.Button(bf, text="查詢", width=10)
    btn_q.pack(pady=1)
    btn_qa = tk.Button(bf, text="全部重查", width=10)
    btn_qa.pack(pady=1)
    btn_set = tk.Button(bf, text="設定", width=10)
    btn_set.pack(pady=1)

    tk.Label(root, text="例: 7Q56-478  0935  22691670-000 (空白或 Tab 分隔,可直接從 Excel 貼上多行)",
             fg="gray").grid(row=1, column=1, sticky="w")
    tk.Label(root, textvariable=vroots, fg="gray", justify="left", anchor="w"
             ).grid(row=2, column=1, columnspan=2, sticky="w", padx=2)

    # ---- 查詢結果清單 ----
    tk.Label(root, textvariable=vsum, anchor="w", font=("", 10, "bold")
             ).grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(6, 0))
    ff = tk.Frame(root)
    ff.grid(row=4, column=0, columnspan=3, padx=8, pady=4, sticky="nsew")
    ff.columnconfigure(0, weight=1)
    root.rowconfigure(4, weight=1)
    cols = ("no", "part", "suffix", "lot", "status", "msg")
    tree = ttk.Treeview(ff, columns=cols, show="headings", height=8)
    for c, t, w in (("no", "行", 40), ("part", "料號", 110), ("suffix", "尾碼", 60),
                    ("lot", "批號", 130), ("status", "狀態", 80), ("msg", "說明", 520)):
        tree.heading(c, text=t)
        tree.column(c, width=w, anchor="w", stretch=(c == "msg"))
    tvs = tk.Scrollbar(ff, command=tree.yview)
    tree.configure(yscrollcommand=tvs.set)
    tree.grid(row=0, column=0, sticky="nsew")
    tvs.grid(row=0, column=1, sticky="ns")
    tree.tag_configure("ok", foreground="#1a7f37")
    tree.tag_configure("bad", foreground="#c62828")
    tree.tag_configure("dup", foreground="gray")
    tree.tag_configure("busy", foreground="gray")

    # ---- 輸出與執行 ----
    cf = tk.Frame(root)
    cf.grid(row=5, column=0, columnspan=3, sticky="ew", padx=8)
    cf.columnconfigure(1, weight=1)
    tk.Label(cf, text="C 輸出位置", width=12, anchor="e").grid(row=0, column=0, padx=4, pady=4)
    tk.Entry(cf, textvariable=vo).grid(row=0, column=1, sticky="ew")

    def pick_o():
        p = filedialog.askdirectory(title="選擇 C 輸出位置", initialdir=cfg.get("dir_c") or None)
        if p:
            vo.set(p)
            cfg["dir_c"] = p
            save_config(cfg)

    tk.Button(cf, text="選擇…", command=pick_o).grid(row=0, column=2, padx=6)

    rf = tk.Frame(root)
    rf.grid(row=6, column=0, columnspan=3, pady=2)
    btn_go = tk.Button(rf, text="開始轉換", width=16, state="disabled")
    btn_go.grid(row=0, column=0, padx=6)
    bar = ttk.Progressbar(rf, mode="indeterminate", length=380)
    bar.grid(row=0, column=1, padx=6)
    tk.Label(rf, textvariable=vprog, width=26, anchor="w").grid(row=0, column=2)

    box = scrolledtext.ScrolledText(root, height=9, state="disabled")
    box.grid(row=7, column=0, columnspan=3, padx=8, pady=8, sticky="nsew")

    def log_ui(msg):
        box.configure(state="normal")
        box.insert("end", msg + "\n")
        box.see("end")
        box.configure(state="disabled")

    # ---- 狀態切換 ----
    def refresh():
        busy = state["busy"]
        txt.configure(state="disabled" if busy else "normal")
        for b in (btn_q, btn_qa, btn_set):
            b.configure(state="disabled" if busy else "normal")
        rows = state["rows"]
        can_go = bool(rows) and any(r["ok"] for r in rows) and not busy
        btn_go.configure(state="normal" if can_go else "disabled")

    def set_busy(b):
        state["busy"] = b
        if b:
            bar.configure(mode="indeterminate", value=0)
            bar.start(10)
        else:
            bar.stop()
        refresh()

    def invalidate(note):
        """輸入或設定變更:舊的查詢結果不能再拿來轉換。"""
        if state["rows"] is not None:
            state["rows"] = None
            vsum.set(note)
            refresh()

    def on_modified(_evt=None):
        if txt.edit_modified():
            txt.edit_modified(False)
            invalidate("輸入已變更,請重新查詢")

    txt.bind("<<Modified>>", on_modified)

    def set_row(iid, status_text, tag, msg):
        tree.set(iid, "status", status_text)
        tree.set(iid, "msg", msg)
        tree.item(iid, tags=(tag,))
        state["msgs"][iid] = msg

    # ---- 查詢 ----
    def worker_query(jobs, a_root, b_root):
        def one(job):
            iid, key = job
            ck = (a_root, b_root) + key
            if ck in state["cache"]:
                q.put(("qrow", iid, ck, True, state["cache"][ck], True))
                return
            try:
                q.put(("qrow", iid, ck, True, inspect_pair(*key, a_root, b_root), False))
            except Exception as e:
                q.put(("qrow", iid, ck, False, str(e), False))
        with ThreadPoolExecutor(max_workers=4) as ex:  # 網路延遲為主,平行查可加快
            list(ex.map(one, jobs))
        q.put(("qdone",))

    def finish_query():
        stat = [("ok" if r["ok"] else r["status"]) for r in state["rows"]]
        vsum.set(summary_text(stat))
        set_busy(False)

    def do_query(force=False):
        if state["busy"]:
            return
        if force:
            state["cache"].clear()
        entries = parse_lines(txt.get("1.0", "end"))
        tree.delete(*tree.get_children())
        state["msgs"].clear()
        if not entries:
            state["rows"] = None
            vsum.set("沒有輸入內容")
            refresh()
            return
        a_root, b_root = get_roots(cfg)
        rows, jobs = [], []
        for e in entries:
            if e["key"]:
                p, s, l = e["key"]
            else:
                p, s, l = e["raw"], "", ""
            if e["status"] is None:
                st, tag, msg = "查詢中", "busy", "查詢中(網路磁碟可能較慢)..."
            elif e["status"] == "bad":
                st, tag, msg = "有問題", "bad", e["msg"]
            else:
                st, tag, msg = "重複", "dup", e["msg"]
            iid = tree.insert("", "end", values=(e["no"], p, s, l, st, msg), tags=(tag,))
            state["msgs"][iid] = msg
            rows.append({"iid": iid, "status": e["status"] or "pending",
                         "ok": False, "info": None})
            if e["status"] is None:
                jobs.append((iid, e["key"]))
        state["rows"] = rows
        vsum.set("查詢中...")
        set_busy(True)
        if jobs:
            threading.Thread(target=worker_query, args=(jobs, a_root, b_root),
                             daemon=True).start()
        else:
            finish_query()

    # ---- 轉換 ----
    def worker_convert(items, out):
        total, res = len(items), {"done": 0, "partial": 0, "fail": 0}
        for g, (iid, info) in enumerate(items, 1):
            q.put(("grp", g, total, info["lot"]))
            tag = f"[{info['lot']}] "
            try:
                _, ok, failed = convert_via_local(
                    info["a_zip"], info["b_dir"], out,
                    log=lambda m, t=tag: q.put(("log", t + m)),
                    progress=lambda i, n: q.put(("prog", (i, n))))
                kind = "partial" if failed else "done"
                res[kind] += 1
                q.put(("grpres", iid, kind, ok, len(failed)))
            except Exception as e:
                res["fail"] += 1
                q.put(("log", tag + f"錯誤: {e}"))
                q.put(("grpres", iid, "fail", 0, str(e)))
        q.put(("cdone", res))

    def do_go():
        rows = state["rows"]
        if state["busy"] or not rows:
            return
        out = vo.get().strip()
        if not out:
            messagebox.showwarning("提示", "請先選擇 C 輸出位置")
            return
        items = [(r["iid"], r["info"]) for r in rows if r["ok"]]
        if not items:
            return
        log_ui("=" * 40)
        log_ui(f"開始轉換 {len(items)} 組")
        set_busy(True)
        threading.Thread(target=worker_convert, args=(items, out), daemon=True).start()

    # ---- 設定視窗 ----
    def open_settings():
        if state["busy"]:
            return
        dlg = tk.Toplevel(root)
        dlg.title("路徑設定")
        dlg.transient(root)
        dlg.resizable(False, False)
        a_cur, b_cur = get_roots(cfg)
        va, vb = tk.StringVar(value=a_cur), tk.StringVar(value=b_cur)
        ta, tb = tk.StringVar(), tk.StringVar()
        state["test"] = {"a": ta, "b": tb}

        def pick(var):
            p = filedialog.askdirectory(title="選擇資料夾", initialdir=var.get() or None,
                                        parent=dlg)
            if p:
                var.set(os.path.normpath(p))

        def clean(v):
            p = v.get().strip().strip('"')
            return p.rstrip("\\/") if len(p) > 3 else p

        def test():
            ta.set("測試中...")
            tb.set("測試中...")
            for k, v in (("a", va), ("b", vb)):
                threading.Thread(
                    target=lambda k=k, p=clean(v): q.put(("test", k, os.path.isdir(p))),
                    daemon=True).start()

        for r, (label, var, tv) in enumerate((("A 根路徑", va, ta), ("B 根路徑", vb, tb))):
            tk.Label(dlg, text=label, width=10, anchor="e").grid(row=r * 2, column=0, padx=6, pady=(10, 0))
            tk.Entry(dlg, textvariable=var, width=70).grid(row=r * 2, column=1, pady=(10, 0))
            tk.Button(dlg, text="選擇…", command=lambda v=var: pick(v)).grid(row=r * 2, column=2, padx=6, pady=(10, 0))
            tk.Label(dlg, textvariable=tv, fg="gray", anchor="w").grid(row=r * 2 + 1, column=1, sticky="w")

        def default():
            va.set(DEFAULT_A_ROOT)
            vb.set(DEFAULT_B_ROOT)

        def save():
            a, b = clean(va), clean(vb)
            if not a or not b:
                messagebox.showwarning("提示", "A、B 根路徑都不能是空的", parent=dlg)
                return
            cfg["root_a"], cfg["root_b"] = a, b
            save_config(cfg)
            update_roots_label()
            state["cache"].clear()
            tree.delete(*tree.get_children())
            state["rows"] = None
            vsum.set("路徑設定已變更,請重新查詢")
            refresh()
            close()

        def close():
            state["test"] = None
            dlg.destroy()

        bb = tk.Frame(dlg)
        bb.grid(row=4, column=0, columnspan=3, pady=12)
        tk.Button(bb, text="測試連線", width=10, command=test).pack(side="left", padx=4)
        tk.Button(bb, text="恢復預設", width=10, command=default).pack(side="left", padx=4)
        tk.Button(bb, text="儲存", width=10, command=save).pack(side="left", padx=4)
        tk.Button(bb, text="取消", width=10, command=close).pack(side="left", padx=4)
        dlg.protocol("WM_DELETE_WINDOW", close)
        dlg.grab_set()

    # ---- 主執行緒統一處理背景訊息 ----
    def poll():
        try:
            while True:
                msg = q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    log_ui(msg[1])
                elif kind == "qrow":
                    _, iid, ck, ok, val, cached = msg
                    row = next((r for r in (state["rows"] or []) if r["iid"] == iid), None)
                    if row is None:
                        continue
                    if ok:
                        row["ok"], row["info"], row["status"] = True, val, "ok"
                        state["cache"][ck] = val
                        set_row(iid, "通過", "ok", row_message(val))
                    else:
                        row["status"] = "bad"
                        set_row(iid, "有問題", "bad", val)
                elif kind == "qdone":
                    finish_query()
                elif kind == "grp":
                    _, g, total, lot = msg
                    bar.configure(mode="indeterminate", value=0)
                    bar.start(10)
                    vprog.set(f"第 {g} / {total} 組 ({lot})")
                elif kind == "prog":
                    bar.stop()
                    bar.configure(mode="determinate", maximum=msg[1][1])
                    bar["value"] = msg[1][0]
                elif kind == "grpres":
                    _, iid, k, a, b = msg
                    if k == "done":
                        set_row(iid, "已轉換", "ok", f"已轉換 {a} 片")
                    elif k == "partial":
                        set_row(iid, "部分失敗", "bad", f"已轉換 {a} 片,{b} 片失敗(詳見下方記錄)")
                    else:
                        set_row(iid, "轉換失敗", "bad", f"轉換失敗: {b}")
                elif kind == "cdone":
                    res = msg[1]
                    text = (f"全部完成: 成功 {res['done']} 組,部分失敗 {res['partial']} 組,"
                            f"失敗 {res['fail']} 組")
                    log_ui(text)
                    vprog.set("")
                    set_busy(False)
                    bar["value"] = 0
                    if res["partial"] or res["fail"]:
                        messagebox.showwarning("完成(有問題)", text)
                    else:
                        messagebox.showinfo("完成", text)
                elif kind == "test":
                    t = state["test"]
                    if t:
                        t[msg[1]].set("連線正常" if msg[2] else "連不到或不是資料夾")
        except queue.Empty:
            pass
        root.after(100, poll)

    def on_double(_evt=None):
        sel = tree.selection()
        if sel:
            m = state["msgs"].get(sel[0], "")
            if m:
                messagebox.showinfo("詳細說明", m)

    tree.bind("<Double-1>", on_double)
    btn_q.configure(command=lambda: do_query(False))
    btn_qa.configure(command=lambda: do_query(True))
    btn_set.configure(command=open_settings)
    btn_go.configure(command=do_go)
    update_roots_label()
    root.after(100, poll)
    txt.focus_set()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 4:   # 舊式命令列: python B2C7.py A.zip B資料夾 輸出資料夾
        convert_via_local(sys.argv[1], sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3:  # python B2C7.py "7Q56-478 0935 22691670-000" 輸出資料夾
        a_root, b_root = get_roots(load_config())
        info = inspect_pair(*parse_input(sys.argv[1]), a_root, b_root)
        print(row_message(info))
        convert_via_local(info["a_zip"], info["b_dir"], sys.argv[2])
    else:
        run_gui()
