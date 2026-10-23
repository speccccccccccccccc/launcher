#!/usr/bin/env python3
"""
Простейший лаунчер Minecraft.
Возможности: выбор версии, Vanilla / Fabric / Forge, пиратка или лицензия (Microsoft),
никнейм, сборки (отдельные папки с модами/мирами), консоль с логом.

Установка:  pip install minecraft-launcher-lib
Запуск:     python simple_launcher.py
"""
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

try:
    import minecraft_launcher_lib as mll
except ImportError:
    r = tk.Tk()
    r.withdraw()
    messagebox.showerror(
        "Нет библиотеки",
        "Установите зависимость командой:\n\npip install minecraft-launcher-lib",
    )
    sys.exit(1)

if getattr(sys, "frozen", False):   # запуск как .exe (PyInstaller)
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
BASE = APP_DIR / "launcher_data"
MC_DIR = BASE / "minecraft"      # общие библиотеки, ассеты, версии
INST_DIR = BASE / "instances"    # папки сборок (моды, миры, настройки)
CFG_FILE = BASE / "config.json"
REDIRECT = "http://localhost"

DEFAULT_CFG = {
    "mode": "offline",       # offline | licensed
    "nick": "Player",
    "client_id": "",         # Azure Client ID для входа в Microsoft
    "account": None,         # данные лицензионного аккаунта
    "profiles": [],
    "last": "",
}


def load_cfg():
    BASE.mkdir(exist_ok=True)
    MC_DIR.mkdir(exist_ok=True)
    INST_DIR.mkdir(exist_ok=True)
    cfg = dict(DEFAULT_CFG)
    if CFG_FILE.exists():
        try:
            cfg.update(json.loads(CFG_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def save_cfg(cfg):
    CFG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def offline_uuid(name: str) -> str:
    """UUID как у офлайн-режима самого Minecraft."""
    h = hashlib.md5(("OfflinePlayer:" + name).encode("utf-8")).digest()
    return str(uuid.UUID(bytes=h, version=3))


def open_folder(path: Path):
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(str(path))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ----------------------------------------------------------------- диалог сборки
class NewProfileDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Создать сборку")
        self.resizable(False, False)
        self.transient(app)
        self.grab_set()

        self.name = tk.StringVar()
        self.loader = tk.StringVar(value="Vanilla")
        self.snap = tk.BooleanVar(value=False)
        self.version = tk.StringVar()
        self.ram = tk.IntVar(value=2048)

        pad = {"padx": 8, "pady": 4}
        ttk.Label(self, text="Название:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.name, width=30).grid(row=0, column=1, **pad)

        ttk.Label(self, text="Загрузчик:").grid(row=1, column=0, sticky="w", **pad)
        lc = ttk.Combobox(self, textvariable=self.loader, state="readonly",
                          values=["Vanilla", "Fabric", "Forge"], width=27)
        lc.grid(row=1, column=1, **pad)
        lc.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        ttk.Label(self, text="Версия:").grid(row=2, column=0, sticky="w", **pad)
        self.vc = ttk.Combobox(self, textvariable=self.version, state="readonly", width=27)
        self.vc.grid(row=2, column=1, **pad)

        ttk.Checkbutton(self, text="Показывать снапшоты", variable=self.snap,
                        command=self.refresh).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(self, text="ОЗУ (МБ):").grid(row=4, column=0, sticky="w", **pad)
        ttk.Spinbox(self, from_=512, to=32768, increment=512, textvariable=self.ram,
                    width=10).grid(row=4, column=1, sticky="w", **pad)

        bf = ttk.Frame(self)
        bf.grid(row=5, column=0, columnspan=2, pady=10)
        ttk.Button(bf, text="Создать", command=self.create).pack(side="left", padx=5)
        ttk.Button(bf, text="Отмена", command=self.destroy).pack(side="left", padx=5)
        self.refresh()

    def refresh(self):
        loader = self.loader.get().lower()
        items = []
        for vid, vtype in self.app.vanilla:
            if vtype == "release" or (self.snap.get() and loader != "forge" and vtype == "snapshot"):
                if loader == "fabric" and vid not in self.app.fabric_ids:
                    continue
                items.append(vid)
        self.vc["values"] = items
        if items:
            self.version.set(items[0])

    def create(self):
        raw = self.name.get().strip()
        name = re.sub(r'[\\/:*?"<>|]', "_", raw)
        if not name:
            return messagebox.showwarning("Ошибка", "Введите название сборки", parent=self)
        if any(p["name"] == name for p in self.app.cfg["profiles"]):
            return messagebox.showwarning("Ошибка", "Сборка с таким названием уже есть", parent=self)
        if not self.version.get():
            return messagebox.showwarning("Ошибка", "Выберите версию", parent=self)
        try:
            ram = max(512, int(self.ram.get()))
        except Exception:
            ram = 2048
        prof = {"name": name, "mc": self.version.get(), "loader": self.loader.get().lower(),
                "ram": ram, "version_id": None}
        (INST_DIR / name).mkdir(parents=True, exist_ok=True)
        self.app.cfg["profiles"].append(prof)
        self.app.cfg["last"] = name
        save_cfg(self.app.cfg)
        self.app.refresh_profiles()
        self.destroy()


# ----------------------------------------------------------------- главное окно
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Простой лаунчер Minecraft")
        self.geometry("860x640")
        self.minsize(720, 520)

        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.proc = None
        self.busy = False
        self.vanilla = []        # [(id, type)]
        self.fabric_ids = set()

        self.build_ui()
        self.refresh_profiles()
        self.on_mode()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.pump)
        threading.Thread(target=self.load_versions, daemon=True).start()

    # ---- интерфейс
    def build_ui(self):
        pad = {"padx": 8, "pady": 4}

        acc = ttk.LabelFrame(self, text="Аккаунт")
        acc.pack(fill="x", padx=10, pady=(10, 4))
        self.mode = tk.StringVar(value=self.cfg["mode"])
        ttk.Radiobutton(acc, text="Пиратка (офлайн)", variable=self.mode, value="offline",
                        command=self.on_mode).grid(row=0, column=0, sticky="w", **pad)
        ttk.Radiobutton(acc, text="Лицензия (Microsoft)", variable=self.mode, value="licensed",
                        command=self.on_mode).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(acc, text="Ник:").grid(row=1, column=0, sticky="e", **pad)
        self.nick = tk.StringVar(value=self.cfg["nick"])
        self.nick_entry = ttk.Entry(acc, textvariable=self.nick, width=24)
        self.nick_entry.grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(acc, text="Azure Client ID:").grid(row=2, column=0, sticky="e", **pad)
        self.cid = tk.StringVar(value=self.cfg["client_id"])
        self.cid_entry = ttk.Entry(acc, textvariable=self.cid, width=42)
        self.cid_entry.grid(row=2, column=1, sticky="w", **pad)
        self.login_btn = ttk.Button(acc, text="Войти в Microsoft", command=self.ms_login)
        self.login_btn.grid(row=2, column=2, **pad)
        self.acc_label = ttk.Label(acc, text="")
        self.acc_label.grid(row=1, column=2, sticky="w", **pad)

        pf = ttk.LabelFrame(self, text="Сборка")
        pf.pack(fill="x", padx=10, pady=4)
        self.prof_var = tk.StringVar()
        self.prof_box = ttk.Combobox(pf, textvariable=self.prof_var, state="readonly", width=30)
        self.prof_box.grid(row=0, column=0, **pad)
        self.prof_box.bind("<<ComboboxSelected>>", lambda e: self.on_profile())
        ttk.Button(pf, text="Создать", command=self.new_profile).grid(row=0, column=1, **pad)
        ttk.Button(pf, text="Удалить", command=self.del_profile).grid(row=0, column=2, **pad)
        ttk.Button(pf, text="Папка", command=self.open_prof_dir).grid(row=0, column=3, **pad)
        self.prof_info = ttk.Label(pf, text="")
        self.prof_info.grid(row=1, column=0, columnspan=4, sticky="w", **pad)

        lf = ttk.Frame(self)
        lf.pack(fill="x", padx=10, pady=4)
        self.play_btn = ttk.Button(lf, text="▶ Играть", command=self.play)
        self.play_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(lf, text="■ Стоп", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.status = tk.StringVar(value="Готов")
        ttk.Label(lf, textvariable=self.status).pack(side="left", padx=12)
        self.bar = ttk.Progressbar(self, mode="determinate")
        self.bar.pack(fill="x", padx=14, pady=2)

        cf = ttk.LabelFrame(self, text="Консоль")
        cf.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.console = tk.Text(cf, bg="#111", fg="#ddd", insertbackground="#ddd",
                               font=("Consolas", 10), wrap="none", state="disabled")
        sy = ttk.Scrollbar(cf, orient="vertical", command=self.console.yview)
        sx = ttk.Scrollbar(cf, orient="horizontal", command=self.console.xview)
        self.console.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.console.pack(fill="both", expand=True)
        ttk.Button(self, text="Очистить консоль", command=self.clear_console).pack(pady=(0, 8))

    # ---- консоль и очередь
    def log(self, msg):
        self.q.put(("log", str(msg)))

    def clear_console(self):
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")

    def pump(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self.console.configure(state="normal")
                    self.console.insert("end", item[1] + "\n")
                    if int(self.console.index("end-1c").split(".")[0]) > 6000:
                        self.console.delete("1.0", "1000.0")
                    self.console.see("end")
                    self.console.configure(state="disabled")
                elif kind == "status":
                    self.status.set(item[1])
                elif kind == "progress":
                    self.bar["maximum"] = max(item[2], 1)
                    self.bar["value"] = item[1]
                elif kind == "busy":
                    self.busy = item[1]
                    self.play_btn.configure(state="disabled" if item[1] else "normal")
                    if not item[1]:
                        self.stop_btn.configure(state="disabled")
                        self.bar["value"] = 0
                elif kind == "running":
                    self.stop_btn.configure(state="normal" if item[1] else "disabled")
                elif kind == "versions_ready":
                    pass
        except queue.Empty:
            pass
        self.after(100, self.pump)

    # ---- версии
    def load_versions(self):
        try:
            self.log("Загружаю список версий...")
            self.vanilla = [(v["id"], v["type"]) for v in mll.utils.get_version_list()]
            try:
                fab = mll.fabric.get_all_minecraft_versions()
                self.fabric_ids = {(v["version"] if isinstance(v, dict) else v) for v in fab}
            except Exception as e:
                self.log(f"Не удалось получить версии Fabric: {e}")
            self.log(f"Версий загружено: {len(self.vanilla)}")
        except Exception as e:
            self.log(f"Ошибка загрузки версий (нет интернета?): {e}")

    # ---- аккаунт
    def on_mode(self):
        lic = self.mode.get() == "licensed"
        self.nick_entry.configure(state="disabled" if lic else "normal")
        self.cid_entry.configure(state="normal" if lic else "disabled")
        self.login_btn.configure(state="normal" if lic else "disabled")
        acc = self.cfg.get("account")
        if lic:
            self.acc_label.configure(text=f"Вошли как: {acc['name']}" if acc else "Не выполнен вход")
        else:
            self.acc_label.configure(text="")

    def ms_login(self):
        cid = self.cid.get().strip()
        if not cid:
            return messagebox.showwarning(
                "Нужен Client ID",
                "Зарегистрируйте приложение в Azure (Mobile and desktop, redirect "
                f"{REDIRECT}) и вставьте его Application (client) ID.")
        try:
            ms = mll.microsoft_account
            url, state, verifier = ms.get_secure_login_data(cid, REDIRECT)
            webbrowser.open(url)
            redirected = simpledialog.askstring(
                "Вход в Microsoft",
                "Войдите в браузере. Страница localhost может не открыться — это нормально.\n"
                "Скопируйте полный адрес из адресной строки и вставьте сюда:",
                parent=self)
            if not redirected:
                return
            code = ms.parse_auth_code_url(redirected.strip(), state)
            self.config(cursor="watch")
            self.update_idletasks()
            acc = ms.complete_login(cid, None, REDIRECT, code, verifier)
            self.cfg["account"] = {k: acc[k] for k in ("id", "name", "access_token", "refresh_token")}
            self.cfg["client_id"] = cid
            save_cfg(self.cfg)
            self.log(f"Вход выполнен: {acc['name']}")
        except Exception as e:
            self.log(f"Ошибка входа: {e}")
            messagebox.showerror("Ошибка входа", str(e))
        finally:
            self.config(cursor="")
            self.on_mode()

    # ---- сборки
    def current_profile(self):
        for p in self.cfg["profiles"]:
            if p["name"] == self.prof_var.get():
                return p
        return None

    def refresh_profiles(self):
        names = [p["name"] for p in self.cfg["profiles"]]
        self.prof_box["values"] = names
        if self.cfg["last"] in names:
            self.prof_var.set(self.cfg["last"])
        elif names:
            self.prof_var.set(names[0])
        else:
            self.prof_var.set("")
        self.on_profile()

    def on_profile(self):
        p = self.current_profile()
        if p:
            self.cfg["last"] = p["name"]
            self.prof_info.configure(
                text=f"Minecraft {p['mc']} • {p['loader'].capitalize()} • ОЗУ {p['ram']} МБ")
        else:
            self.prof_info.configure(text="Сборок нет — нажмите «Создать»")

    def new_profile(self):
        if not self.vanilla:
            return messagebox.showinfo("Подождите", "Список версий ещё не загружен.")
        NewProfileDialog(self)

    def del_profile(self):
        p = self.current_profile()
        if not p:
            return
        if not messagebox.askyesno("Удаление", f"Удалить сборку «{p['name']}»?"):
            return
        self.cfg["profiles"].remove(p)
        if messagebox.askyesno("Удаление", "Удалить также папку сборки (миры, моды)?"):
            shutil.rmtree(INST_DIR / p["name"], ignore_errors=True)
        save_cfg(self.cfg)
        self.refresh_profiles()

    def open_prof_dir(self):
        p = self.current_profile()
        if p:
            open_folder(INST_DIR / p["name"])

    # ---- запуск
    def play(self):
        p = self.current_profile()
        if not p:
            return messagebox.showwarning("Нет сборки", "Сначала создайте сборку.")
        mode = self.mode.get()
        nick = self.nick.get().strip()
        if mode == "offline" and not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nick):
            return messagebox.showwarning("Ник", "Ник: 3–16 символов, латиница, цифры и _")
        if mode == "licensed" and not self.cfg.get("account"):
            return messagebox.showwarning("Лицензия", "Сначала войдите в Microsoft.")
        self.cfg.update(mode=mode, nick=nick or self.cfg["nick"], client_id=self.cid.get().strip())
        save_cfg(self.cfg)
        self.q.put(("busy", True))
        threading.Thread(target=self.worker, args=(p, mode, nick), daemon=True).start()

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.log("Процесс игры остановлен.")

    def callback(self):
        st = {"max": 0}
        return {
            "setStatus": lambda t: self.q.put(("status", str(t))),
            "setMax": lambda m: st.__setitem__("max", m),
            "setProgress": lambda v: self.q.put(("progress", v, st["max"])),
        }

    def installed_ids(self):
        return {v["id"] for v in mll.utils.get_installed_versions(str(MC_DIR))}

    def ensure_installed(self, p):
        d, mc, loader = str(MC_DIR), p["mc"], p["loader"]
        cb = self.callback()
        if p.get("version_id") and p["version_id"] in self.installed_ids():
            return p["version_id"]

        self.log(f"Устанавливаю Minecraft {mc}...")
        mll.install.install_minecraft_version(mc, d, callback=cb)
        before = self.installed_ids()

        if loader == "vanilla":
            vid = mc
        elif loader == "fabric":
            self.log("Устанавливаю Fabric...")
            mll.fabric.install_fabric(mc, d, callback=cb)
            after = self.installed_ids()
            new = [i for i in after - before if i.startswith("fabric-loader")]
            if not new:
                new = sorted(i for i in after if i.startswith("fabric-loader-") and i.endswith("-" + mc))
            if not new:
                raise RuntimeError("Не удалось определить версию Fabric")
            vid = new[-1]
        else:  # forge
            fv = mll.forge.find_forge_version(mc)
            if not fv:
                raise RuntimeError(f"Для Minecraft {mc} нет версии Forge")
            if not mll.forge.supports_automatic_install(fv):
                raise RuntimeError(f"Forge {fv} не поддерживает автоустановку")
            if not shutil.which("java"):
                self.log("ВНИМАНИЕ: Java не найдена в PATH, установщику Forge она нужна.")
            self.log(f"Устанавливаю Forge {fv} (может занять пару минут)...")
            mll.forge.install_forge_version(fv, d, callback=cb)
            after = self.installed_ids()
            new = [i for i in after - before if "forge" in i.lower()]
            if not new:
                build = fv.split("-", 1)[1]
                new = [i for i in after if "forge" in i.lower() and build in i]
            if not new:
                raise RuntimeError("Не удалось определить версию Forge")
            vid = new[-1]

        p["version_id"] = vid
        save_cfg(self.cfg)
        return vid

    def get_auth(self, mode, nick):
        if mode == "offline":
            return nick, offline_uuid(nick), "0"
        acc = self.cfg["account"]
        try:
            self.log("Обновляю сессию Microsoft...")
            new = mll.microsoft_account.complete_refresh(
                self.cfg["client_id"], None, REDIRECT, acc["refresh_token"])
            acc = {k: new[k] for k in ("id", "name", "access_token", "refresh_token")}
            self.cfg["account"] = acc
            save_cfg(self.cfg)
        except Exception as e:
            raise RuntimeError(f"Сессия истекла, войдите заново ({e})")
        return acc["name"], acc["id"], acc["access_token"]

    def worker(self, p, mode, nick):
        try:
            vid = self.ensure_installed(p)
            name, uid, token = self.get_auth(mode, nick)
            game_dir = INST_DIR / p["name"]
            game_dir.mkdir(parents=True, exist_ok=True)
            ram = int(p["ram"])
            options = {
                "username": name, "uuid": uid, "token": token,
                "gameDirectory": str(game_dir),
                "jvmArguments": [f"-Xmx{ram}M", f"-Xms{min(512, ram)}M"],
                "launcherName": "SimpleLauncher",
            }
            cmd = mll.command.get_minecraft_command(vid, str(MC_DIR), options)
            self.log(f"Запуск {vid} как «{name}» ({'лицензия' if mode == 'licensed' else 'офлайн'})")
            self.q.put(("status", "Игра запущена"))
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.proc = subprocess.Popen(
                cmd, cwd=str(game_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", creationflags=flags)
            self.q.put(("running", True))
            for line in self.proc.stdout:
                self.log(line.rstrip())
            code = self.proc.wait()
            self.log(f"Игра завершилась с кодом {code}")
        except Exception as e:
            self.log(f"ОШИБКА: {e}")
        finally:
            self.proc = None
            self.q.put(("running", False))
            self.q.put(("status", "Готов"))
            self.q.put(("busy", False))

    def on_close(self):
        self.cfg["nick"] = self.nick.get().strip() or self.cfg["nick"]
        self.cfg["mode"] = self.mode.get()
        self.cfg["client_id"] = self.cid.get().strip()
        save_cfg(self.cfg)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
