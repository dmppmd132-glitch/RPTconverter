#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B轉C(參考A)

A : 客戶進貨資料(.zip,內含 .rpt;有無內層資料夾皆可)
B : 廠內完成資料(資料夾,內含 .txt,0=缺點 1=良品)
C : 轉換資料(.zip,檔名=B資料夾名稱,壓縮檔內是同名資料夾,資料夾內才是 .rpt)

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
import zipfile
from datetime import datetime

NL = "\r\n"
MACHINE = "GCSS-3100"
ENC = "latin1"  # 位元組原樣保留


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


def convert(a_zip, b_dir, out_dir, log=print):
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
        for fname, stem in zip(b_files, b_stems):
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
def run_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, scrolledtext

    root = tk.Tk()
    root.title("B轉C(參考A)")
    root.geometry("720x460")

    va, vb, vo = tk.StringVar(), tk.StringVar(), tk.StringVar()

    def row(r, label, var, cmd):
        tk.Label(root, text=label, width=14, anchor="e").grid(row=r, column=0, padx=6, pady=6)
        tk.Entry(root, textvariable=var, width=60).grid(row=r, column=1)
        tk.Button(root, text="選擇…", command=cmd).grid(row=r, column=2, padx=6)

    def pick_a():
        p = filedialog.askopenfilename(title="選擇 A 壓縮檔", filetypes=[("ZIP", "*.zip")])
        if p:
            va.set(p)

    def pick_b():
        p = filedialog.askdirectory(title="選擇 B 資料夾")
        if p:
            vb.set(p)
            if not vo.get():  # 預設輸出在 B 資料夾的上一層
                vo.set(os.path.dirname(os.path.normpath(p)))

    def pick_o():
        p = filedialog.askdirectory(title="選擇 C 輸出位置")
        if p:
            vo.set(p)

    row(0, "A 壓縮檔", va, pick_a)
    row(1, "B 資料夾", vb, pick_b)
    row(2, "C 輸出位置", vo, pick_o)

    box = scrolledtext.ScrolledText(root, height=18, state="disabled")
    box.grid(row=4, column=0, columnspan=3, padx=8, pady=8, sticky="nsew")

    def log(msg):
        box.configure(state="normal")
        box.insert("end", msg + "\n")
        box.see("end")
        box.configure(state="disabled")
        root.update_idletasks()

    def go():
        if not (va.get() and vb.get() and vo.get()):
            messagebox.showwarning("提示", "請先選擇 A、B 與輸出位置")
            return
        try:
            convert(va.get(), vb.get(), vo.get(), log)
        except Exception as e:
            log(f"錯誤: {e}")
            messagebox.showerror("錯誤", str(e))

    tk.Button(root, text="開始轉換", width=16, command=go).grid(row=3, column=1, pady=4)
    root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) == 4:  # 命令列: python b_to_c.py A.zip B資料夾 輸出資料夾
        convert(sys.argv[1], sys.argv[2], sys.argv[3])
    else:
        run_gui()
