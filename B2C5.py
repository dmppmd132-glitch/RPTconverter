#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B轉C(參考A)

A : 客戶進貨資料(.zip,內含 .rpt;有無內層資料夾皆可)
B : 廠內完成資料(資料夾,內含 .txt,0=缺點 1=良品)
C : 轉換資料(.zip,檔名=B資料夾名稱,壓縮檔內是同名資料夾,資料夾內才是 .rpt)

輸入 "料號 尾碼 批號",例如: 7Q56-478  0935  22691670-000
  A = <A_ROOT>\\料號\\COM_料號&尾碼.zip
  B = <B_ROOT>\\料號\\批號
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
import zipfile
from datetime import datetime

NL = "\r\n"
MACHINE = "GCSS-3100"
ENC = "latin1"  # 位元組原樣保留

# ---- 固定路徑設定(路徑變動時只要改這裡)----
A_ROOT = r"\\BARCODE\e\입고 Lot\KINSUS"
B_ROOT = r"\\BARCODE\e\Final\Sorter\Sorting2\KINSUS"


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
def stage_local(a_zip, b_dir, tmp_root, log=print, progress=None):
    """把 A 壓縮檔與 B 的 .txt 複製到 tmp_root,回傳 (本機A, 本機B資料夾)。
    本機 B 資料夾名稱與原 B 相同(C 壓縮檔名稱取自此名稱)。"""
    folder = os.path.basename(os.path.normpath(b_dir))

    log("複製 A 壓縮檔到本機...")
    local_a = os.path.join(tmp_root, "A.zip")
    shutil.copyfile(a_zip, local_a)

    txts = sorted(f for f in os.listdir(b_dir) if f.lower().endswith(".txt"))
    local_b = os.path.join(tmp_root, folder)
    os.makedirs(local_b, exist_ok=True)
    log(f"複製 B 資料 {len(txts)} 個到本機...")
    for i, f in enumerate(txts, 1):
        shutil.copyfile(os.path.join(b_dir, f), os.path.join(local_b, f))
        if progress:
            progress(i, len(txts))
    return local_a, local_b


def convert_via_local(a_zip, b_dir, out_dir, log=print, progress=None):
    """複製到本機 -> 轉換 -> 刪除暫存。C 直接輸出到 out_dir。"""
    tmp_root = tempfile.mkdtemp(prefix="b2c_")
    try:
        local_a, local_b = stage_local(a_zip, b_dir, tmp_root, log, progress)
        log("開始轉換...")
        return convert(local_a, local_b, out_dir, log, progress)
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


def parse_input(text):
    """'7Q56-478 0935 22691670-000' -> (料號, 尾碼, 批號)。空白/Tab 皆可。"""
    parts = text.split()
    if len(parts) != 3:
        raise ValueError(
            f"輸入需要三段(料號 尾碼 批號),目前讀到 {len(parts)} 段。\n"
            "例: 7Q56-478  0935  22691670-000")
    for name, p in zip(("料號", "尾碼", "批號"), parts):
        if BAD_CHARS & set(p):
            raise ValueError(f"{name}含有不允許的字元: {p}")
    return tuple(parts)


def build_paths(part, suffix, lot):
    a_zip = os.path.join(A_ROOT, part, f"COM_{part}&{suffix}.zip")
    b_dir = os.path.join(B_ROOT, part, lot)
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


def inspect_pair(part, suffix, lot):
    """找 A、B 並做交叉檢查。有問題一律丟 ValueError(訊息可直接顯示)。"""
    a_zip, b_dir = build_paths(part, suffix, lot)

    if not os.path.isfile(a_zip):
        raise ValueError(f"找不到 A 壓縮檔:\n{a_zip}")
    if not os.path.isdir(b_dir):
        raise ValueError(f"找不到 B 資料夾:\n{b_dir}")

    try:
        a_stems = a_zip_stems(a_zip)
    except zipfile.BadZipFile:
        raise ValueError(f"A 不是有效的壓縮檔(可能還在傳輸中):\n{a_zip}")
    if not a_stems:
        raise ValueError(f"A 壓縮檔內沒有 .rpt:\n{a_zip}")

    # 交叉檢查: 去掉最後五碼後必須等於輸入的批號
    bad = [s for s in a_stems if not STEM_RE.match(s)]
    if bad:
        raise ValueError(
            f"A 內有 {len(bad)} 個檔名不符「批號 空格 四位數」格式,已停止:\n"
            + "\n".join(bad[:5]) + ("\n..." if len(bad) > 5 else ""))
    lots = sorted({STEM_RE.match(s).group("lot") for s in a_stems})
    if lots != [lot]:
        raise ValueError(
            "交叉檢查未通過,已停止。\n"
            f"你輸入的批號: {lot}\n"
            f"A 壓縮檔內的批號: {', '.join(lots)}\n"
            "請確認輸入的尾碼與批號是否屬於同一批。")

    b_stems = sorted(os.path.splitext(f)[0] for f in os.listdir(b_dir)
                     if f.lower().endswith(".txt"))
    if not b_stems:
        raise ValueError(f"B 資料夾內找不到 .txt:\n{b_dir}")

    a_set, b_set = set(a_stems), set(b_stems)
    return {
        "part": part, "suffix": suffix, "lot": lot,
        "a_zip": a_zip, "b_dir": b_dir,
        "a_count": len(a_set), "b_count": len(b_set),
        "convert": len(a_set & b_set),
        "skip": sorted(a_set - b_set),      # A有、B沒有:略過
        "b_only": sorted(b_set - a_set),    # B有、A沒有:會失敗
    }


def format_summary(i):
    lines = [
        f"料號: {i['part']}    尾碼: {i['suffix']}    批號: {i['lot']}",
        f"A: {i['a_zip']}",
        f"B: {i['b_dir']}",
        f"交叉檢查: 通過(A 內 {i['a_count']} 片批號皆為 {i['lot']})",
        f"A 共 {i['a_count']} 片 / B 共 {i['b_count']} 片 -> 將轉換 {i['convert']} 片",
    ]
    if i["skip"]:
        lines.append(f"略過(A有、B沒有) {len(i['skip'])} 片")
    if i["b_only"]:
        lines.append(f"警告: B有、A沒有 {len(i['b_only'])} 片(轉換時會失敗): "
                     + ", ".join(i["b_only"]))
    lines.append("確認無誤後按「開始轉換」。")
    return "\n".join(lines)


def run_gui():
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    root = tk.Tk()
    root.title("B轉C(參考A)")
    root.geometry("760x520")

    cfg = load_config()  # {"dir_c": ...}
    vin = tk.StringVar()
    vo = tk.StringVar(value=cfg.get("dir_c", ""))
    state = {"info": None, "busy": False}
    q = queue.Queue()  # 背景執行緒 -> 介面 的訊息佇列

    tk.Label(root, text="料號 尾碼 批號", width=14, anchor="e").grid(row=0, column=0, padx=6, pady=(10, 2))
    ent = tk.Entry(root, textvariable=vin, width=56)
    ent.grid(row=0, column=1, pady=(10, 2))
    btn_q = tk.Button(root, text="查詢", width=8)
    btn_q.grid(row=0, column=2, padx=6, pady=(10, 2))
    tk.Label(root, text="例: 7Q56-478  0935  22691670-000  (空白或 Tab 分隔)",
             fg="gray").grid(row=1, column=1, sticky="w")

    tk.Label(root, text="C 輸出位置", width=14, anchor="e").grid(row=2, column=0, padx=6, pady=6)
    tk.Entry(root, textvariable=vo, width=56).grid(row=2, column=1)

    def pick_o():
        p = filedialog.askdirectory(title="選擇 C 輸出位置", initialdir=cfg.get("dir_c") or None)
        if p:
            vo.set(p)
            cfg["dir_c"] = p
            save_config(cfg)

    tk.Button(root, text="選擇…", command=pick_o).grid(row=2, column=2, padx=6)

    btn_go = tk.Button(root, text="開始轉換", width=16, state="disabled")
    btn_go.grid(row=3, column=1, pady=4)
    bar = ttk.Progressbar(root, mode="indeterminate", length=420)
    bar.grid(row=4, column=1, pady=2)

    box = scrolledtext.ScrolledText(root, height=18, state="disabled")
    box.grid(row=5, column=0, columnspan=3, padx=8, pady=8, sticky="nsew")

    def log_ui(msg):
        box.configure(state="normal")
        box.insert("end", msg + "\n")
        box.see("end")
        box.configure(state="disabled")

    def clear_ui():
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.configure(state="disabled")

    def refresh():
        busy = state["busy"]
        ent.configure(state="disabled" if busy else "normal")
        btn_q.configure(state="disabled" if busy else "normal")
        btn_go.configure(state="normal" if (state["info"] and not busy) else "disabled")

    def set_busy(b):
        state["busy"] = b
        if b:
            bar.configure(mode="indeterminate", value=0)
            bar.start(10)
        else:
            bar.stop()
        refresh()

    def on_edit(*_):  # 輸入一改,先前的查詢結果就作廢,必須重新查詢
        if state["info"] is not None:
            state["info"] = None
            refresh()

    vin.trace_add("write", on_edit)

    def worker_query(part, suffix, lot):
        try:
            q.put(("checked", inspect_pair(part, suffix, lot)))
        except Exception as e:
            q.put(("fail", str(e)))

    def do_query(_evt=None):
        if state["busy"]:
            return
        clear_ui()
        try:
            part, suffix, lot = parse_input(vin.get())
        except ValueError as e:
            log_ui(f"輸入錯誤: {e}")
            return
        log_ui("查詢中(網路磁碟可能較慢,請稍候)...")
        set_busy(True)
        threading.Thread(target=worker_query, args=(part, suffix, lot), daemon=True).start()

    def worker_convert(a, b, o):
        try:
            convert_via_local(a, b, o,
                              log=lambda m: q.put(("log", m)),
                              progress=lambda i, n: q.put(("prog", (i, n))))
            q.put(("done", None))
        except Exception as e:
            q.put(("error", str(e)))

    def do_go():
        info = state["info"]
        if not info or state["busy"]:
            return
        if not vo.get().strip():
            messagebox.showwarning("提示", "請先選擇 C 輸出位置")
            return
        log_ui("-" * 40)
        set_busy(True)
        threading.Thread(target=worker_convert,
                         args=(info["a_zip"], info["b_dir"], vo.get().strip()),
                         daemon=True).start()

    def poll():  # 在主執行緒定時處理背景訊息,避免跨執行緒操作 Tk
        try:
            while True:
                kind, val = q.get_nowait()
                if kind == "log":
                    log_ui(val)
                elif kind == "prog":
                    bar.stop()
                    bar.configure(mode="determinate", maximum=val[1])
                    bar["value"] = val[0]
                elif kind == "checked":
                    clear_ui()
                    log_ui(format_summary(val))
                    set_busy(False)
                    state["info"] = val
                    refresh()
                elif kind == "fail":
                    clear_ui()
                    log_ui(f"錯誤: {val}")
                    state["info"] = None
                    set_busy(False)
                elif kind == "done":
                    set_busy(False)
                elif kind == "error":
                    log_ui(f"錯誤: {val}")
                    set_busy(False)
                    messagebox.showerror("錯誤", val)
        except queue.Empty:
            pass
        root.after(100, poll)

    btn_q.configure(command=do_query)
    btn_go.configure(command=do_go)
    ent.bind("<Return>", do_query)
    root.after(100, poll)
    ent.focus_set()
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 4:   # 舊式命令列: python B2C5.py A.zip B資料夾 輸出資料夾
        convert_via_local(sys.argv[1], sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3:  # 新式命令列: python B2C5.py "7Q56-478 0935 22691670-000" 輸出資料夾
        info = inspect_pair(*parse_input(sys.argv[1]))
        print(format_summary(info))
        convert_via_local(info["a_zip"], info["b_dir"], sys.argv[2])
    else:
        run_gui()
