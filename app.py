"""WaveBox MP3 — interface locale Windows pour l'audio dont l'export est autorisé.

Dépendances externes uniques : yt-dlp et imageio-ffmpeg (versions + hachages figés).
Aucune télémétrie ni analyse de fichiers n'est envoyée par l'application.
Le téléchargement de vidéos nécessite Internet. Le réencodage local est hors-ligne.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

import imageio_ffmpeg
import yt_dlp

APP_NAME = "WaveBox MP3"
ASSETS = Path(__file__).resolve().parent / "assets"
ICON_PNG = ASSETS / "wavebox_icon.png"
ICON_ICO = ASSETS / "wavebox.ico"
DEFAULT_FOLDER = Path.home() / "Music" / "MP3 YouTube"  # même dossier que l'ancienne version
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "WaveBoxMP3"
CONFIG_FILE = CONFIG_DIR / "preferences.json"
MAX_JOBS = 50
QUALITY_OPTIONS = {
    "320 kb/s · haute qualité": "320k",
    "192 kb/s · compatibilité étendue": "192k",
}
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
TRACK_PREFIX = re.compile(r"^\s*\d{1,3}\s*[-._–]\s*")
TITLE_EXTRAS = re.compile(r"\s*[\[(]\s*(official (?:music )?video|official audio|lyrics?(?: video)?|visuali[sz]er|hd|4k)\s*[\])]\s*$", re.I)
RESERVED = {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)}, *{f"LPT{i}" for i in range(1, 10)}}

BG = "#090E18"
SIDEBAR = "#0B1423"
PANEL = "#121C2B"
PANEL_2 = "#1A2739"
FIELD = "#0D1725"
BORDER = "#29364B"
WHITE = "#F1F3FF"
MUTED = "#A3B1C8"
FAINT = "#70809A"
PURPLE = "#7958DB"
PURPLE_DARK = "#5032B1"
MINT = "#63EDDB"
ORANGE = "#F1B96A"
RED = "#F17F91"
FONT = "Segoe UI"


class Stopped(Exception):
    """L'utilisateur a annulé la tâche."""


def clean_name(value: str, limit: int = 105) -> str:
    name = INVALID_CHARS.sub("-", value).strip(" .")
    name = re.sub(r"\s+", " ", name)[:limit].strip(" .") or "Audio"
    if name.split(".")[0].upper() in RESERVED:
        name = "_" + name
    return name


def numbered_name(index: int, title: str, enabled: bool = True) -> str:
    """Stable 01..50 sequence based on pasted URL order, including skipped failures."""
    label = TITLE_EXTRAS.sub("", title).strip()
    label = TRACK_PREFIX.sub("", label).strip()
    return clean_name(f"{index:02d} - {label}" if enabled else label)


def remove_track_prefix(stem: str) -> str:
    return TRACK_PREFIX.sub("", stem).strip()


def unique_target(folder: Path, name: str) -> Path:
    name = clean_name(name)
    dest = folder / f"{name}.mp3"
    n = 2
    while dest.exists():
        dest = folder / f"{name} ({n}).mp3"
        n += 1
    return dest


def parse_video_url(raw: str) -> str:
    url = raw.strip()
    if not url.startswith(("https://", "http://")):
        raise ValueError("Utilise un lien YouTube complet commençant par https://")
    parts = urlparse(url)
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        raise ValueError("Seuls les liens HTTPS YouTube standards sont acceptés")
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in {"youtu.be", "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        raise ValueError("Le lien doit provenir de youtube.com ou youtu.be")
    if host == "youtu.be" and len(parts.path.strip("/")) < 6:
        raise ValueError("Le lien court ne contient pas d'identifiant vidéo")
    if host != "youtu.be" and not (
        parts.path == "/watch" or parts.path.startswith(("/shorts/", "/live/", "/embed/"))
    ):
        raise ValueError("Utilise le lien direct d'une vidéo, et non d'une chaîne ou playlist")
    if host != "youtu.be" and parts.path == "/watch" and "v=" not in (parts.query or ""):
        raise ValueError("Le lien ne contient pas de paramètre vidéo")
    return url


def media_format_check(path: Path) -> None:
    """Vérifie la signature MP3 et la présence d'au moins un en-tête MPEG Layer III."""
    if not path.is_file() or path.stat().st_size < 3000:
        raise RuntimeError("Le MP3 produit est vide ou anormalement petit")
    with path.open("rb") as f:
        head = f.read(16384)
    offset = 0
    if head.startswith(b"ID3") and len(head) > 10:
        size = sum((head[i] & 0x7F) << shift for i, shift in zip(range(6, 10), (21, 14, 7, 0)))
        offset = 10 + size + (10 if head[5] & 0x10 else 0)
        if offset >= len(head):
            with path.open("rb") as f:
                f.seek(offset)
                head = f.read(8192)
            offset = 0
    found = False
    for i in range(offset, max(offset, len(head) - 4)):
        a, b, c, d = head[i:i+4]
        version = (b >> 3) & 0x03
        layer = (b >> 1) & 0x03
        bitrate_idx = (c >> 4) & 0x0F
        samplerate_idx = (c >> 2) & 0x03
        if a == 0xFF and b & 0xE0 == 0xE0 and version != 1 and layer == 1 and 1 <= bitrate_idx <= 14 and samplerate_idx != 3:
            found = True
            break
    if not found:
        raise RuntimeError("Le fichier généré ne contient pas de trame MP3 reconnue")


def run_checked(command: list[str], cancel: threading.Event, error_file: Path) -> None:
    """Exécution sans shell, annulation possible et récupération des erreurs FFmpeg."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with error_file.open("wb") as err:
        proc = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=err, creationflags=flags)
        while proc.poll() is None:
            if cancel.is_set():
                proc.kill()
                proc.wait()
                raise Stopped()
            time.sleep(0.12)
        code = proc.wait()
    if code:
        details = error_file.read_text(encoding="utf-8", errors="replace").strip()[-700:]
        raise RuntimeError("FFmpeg : " + (details or f"code {code}"))


def encode_and_validate(source: Path, dest_folder: Path, title: str, artist: str,
                        bitrate: str, ffmpeg: str, cancel: threading.Event,
                        send_status=None) -> Path:
    """MP3 CBR 44,1 kHz, stéréo, ID3 v2.3; décodage de test complet avant publication."""
    dest_folder.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wavebox_audio_") as workspace:
        work = Path(workspace)
        output = work / "output.mp3"
        args = [
            ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-map_metadata", "-1",
            "-c:a", "libmp3lame", "-b:a", bitrate, "-ar", "44100", "-ac", "2",
            "-id3v2_version", "3", "-write_id3v1", "1",
            "-metadata", f"title={title}",
        ]
        if artist:
            args += ["-metadata", f"artist={artist}"]
        args += [str(output)]
        run_checked(args, cancel, work / "encode.log")
        if send_status:
            send_status("Vérification du MP3 (lecture complète)…")
        media_format_check(output)
        # Un vrai décodage complet détecte les fichiers tronqués ou corrompus.
        check = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-xerror",
                 "-i", str(output), "-map", "0:a:0", "-f", "null", "-"]
        run_checked(check, cancel, work / "validation.log")
        if cancel.is_set():
            raise Stopped()
        destination = unique_target(dest_folder, title)
        # Copie Windows inter-volumes; le SHA-256 valide l'identité des octets copiés.
        checksum = hashlib.sha256()
        try:
            with output.open("rb") as src, destination.open("xb") as dst:
                while chunk := src.read(1024 * 1024):
                    if cancel.is_set():
                        raise Stopped()
                    dst.write(chunk)
                    checksum.update(chunk)
            if destination.stat().st_size != output.stat().st_size:
                raise RuntimeError("Copie du MP3 incomplète")
            with output.open("rb") as src:
                original_hash = hashlib.file_digest(src, "sha256").digest()
            if checksum.digest() != original_hash:
                raise RuntimeError("Échec du contrôle d'intégrité après copie")
            return destination
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def copy_mp3_checked(source: Path, dest_folder: Path, title: str,
                     ffmpeg: str, cancel: threading.Event, send_status=None) -> Path:
    """Checks MP3 end-to-end and copies bytes losslessly; never modifies source."""
    if not source.is_file() or source.suffix.lower() != ".mp3":
        raise ValueError("Sélectionne uniquement des fichiers MP3 existants")
    media_format_check(source)
    with tempfile.TemporaryDirectory(prefix="wavebox_verify_") as td:
        log = Path(td) / "decode.log"
        if send_status:
            send_status("Contrôle intégral du fichier audio…")
        run_checked([ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-xerror",
                     "-i", str(source), "-map", "0:a:0", "-f", "null", "-"], cancel, log)
    dest_folder.mkdir(parents=True, exist_ok=True)
    target = unique_target(dest_folder, title)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_f, target.open("xb") as output_f:
            while chunk := input_f.read(1024 * 1024):
                if cancel.is_set():
                    raise Stopped()
                output_f.write(chunk)
                digest.update(chunk)
        with source.open("rb") as input_f:
            if hashlib.file_digest(input_f, "sha256").digest() != digest.digest():
                raise RuntimeError("Les octets copiés ne correspondent pas au MP3 d'origine")
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def prefs_load() -> dict:
    try:
        value = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def prefs_save(folder: str, quality: str, numbering: bool) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_DIR / "preferences.tmp"
        tmp.write_text(json.dumps({"folder": folder, "quality": quality, "numbering": numbering}, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_FILE)
    except OSError:
        pass  # L'application fonctionne même si les préférences ne sont pas enregistrables.


class App(tk.Tk):
    """Studio WaveBox : bibliothèque audio et organisation des pistes."""

    def __init__(self) -> None:
        super().__init__()
        self.title("WaveBox Studio 4.0 — Ma bibliothèque audio")
        self.geometry("1350x900")
        self.minsize(1110, 820)
        self.configure(bg=BG)
        self.events: queue.Queue[tuple] = queue.Queue()
        self.stop_flag = threading.Event()
        self.busy = False
        self.pending_close = False
        self.mode = "download"
        self.organize_files: list[Path] = []
        self.last_created: Path | None = None
        p = prefs_load()
        folder = str(p.get("folder") or DEFAULT_FOLDER)
        self.folder_var = tk.StringVar(value=folder)
        quality = str(p.get("quality") or next(iter(QUALITY_OPTIONS)))
        self.quality_var = tk.StringVar(value=quality if quality in QUALITY_OPTIONS else next(iter(QUALITY_OPTIONS)))
        self.numbering_var = tk.BooleanVar(value=bool(p.get("numbering", True)))
        self.status_var = tk.StringVar(value="Tout est prêt pour ton prochain projet audio")
        self.count_var = tk.StringVar(value="00 pistes")
        self.result_var = tk.StringVar(value="Aucun fichier créé pour le moment")
        self.hero_progress = tk.StringVar(value="PRÊT À DÉMARRER")
        self._icon = None
        self._banner = None
        try:
            if ICON_PNG.exists():
                self._icon = tk.PhotoImage(file=str(ICON_PNG))
                self.iconphoto(True, self._icon)
            if sys.platform == "win32" and ICON_ICO.exists():
                self.iconbitmap(str(ICON_ICO))
        except tk.TclError:
            pass
        self._build_styles()
        self._build_ui()
        self.bind("<Configure>", self._adapt_height)
        self.after(110, self._read_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _adapt_height(self, event):
        """Conserve visibles les commandes de suivi sur les écrans plus compacts."""
        if event.widget is self and hasattr(self, "table"):
            rows = 2 if event.height < 860 else 4
            if int(self.table.cget("height")) != rows:
                self.table.configure(height=rows)

    def _build_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Wave.Horizontal.TProgressbar", troughcolor=FIELD, background=MINT,
                        borderwidth=0, lightcolor=MINT, darkcolor=MINT, thickness=8)
        style.configure("Wave.Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=WHITE, borderwidth=0, rowheight=35,
                        relief="flat", font=(FONT, 9))
        style.map("Wave.Treeview", background=[("selected", "#293D59")],
                  foreground=[("selected", WHITE)])
        style.configure("Wave.Treeview.Heading", background=PANEL_2, foreground=MUTED,
                        font=(FONT, 9, "bold"), padding=(10, 11), relief="flat")
        style.map("Wave.Treeview.Heading", background=[("active", PANEL_2)])
        style.configure("Wave.TCombobox", fieldbackground=FIELD, background=PANEL_2,
                        foreground=WHITE, arrowcolor=MINT, bordercolor=BORDER, padding=7)
        style.map("Wave.TCombobox", fieldbackground=[("readonly", FIELD)],
                  foreground=[("readonly", WHITE)], selectbackground=[("readonly", FIELD)],
                  selectforeground=[("readonly", WHITE)])

    def _label(self, parent, text: str, size: int = 10, fg: str = MUTED,
               bold: bool = False, **kwargs):
        return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=fg,
                        font=(FONT, size, "bold" if bold else "normal"), **kwargs)

    def _button(self, parent, text: str, command, primary: bool = False, **kw):
        normal = PURPLE if primary else PANEL_2
        hover = "#9275F5" if primary else "#283750"
        bg = kw.pop("bg", normal)
        btn = tk.Button(parent, text=text, command=command,
                        bg=bg, fg=WHITE, activebackground=hover,
                        activeforeground=WHITE, disabledforeground=FAINT,
                        relief="flat", bd=0, padx=17, pady=10,
                        highlightthickness=0, cursor="hand2",
                        font=(FONT, 10, "bold" if primary else "normal"), **kw)
        btn._normal_bg = bg
        btn._hover_bg = hover
        btn.bind("<Enter>", lambda e, b=btn: b.configure(bg=b._hover_bg)
                 if str(b.cget("state")) == "normal" else None)
        btn.bind("<Leave>", lambda e, b=btn: b.configure(bg=b._normal_bg))
        return btn

    def _card(self, parent, padx=18, pady=16):
        holder = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        content = tk.Frame(holder, bg=PANEL, padx=padx, pady=pady)
        content.pack(fill="both", expand=True)
        return holder, content

    def _setting_label(self, parent, value):
        self._label(parent, value, 9, MUTED, True).pack(anchor="w", pady=(0, 6))

    def _stat(self, parent, value, title, note, accent):
        root = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        root.pack(side="left", fill="both", expand=True, padx=(0, 12))
        inner = tk.Frame(root, bg=PANEL_2, padx=16, pady=11)
        inner.pack(fill="both", expand=True)
        left = tk.Frame(inner, bg=PANEL_2)
        left.pack(side="left", fill="both", expand=True)
        if isinstance(value, tk.Variable):
            tk.Label(left, textvariable=value, bg=PANEL_2, fg=accent,
                     font=(FONT, 17, "bold")).pack(anchor="w")
        else:
            self._label(left, value, 17, accent, True).pack(anchor="w")
        self._label(left, title, 9, WHITE, True).pack(anchor="w", pady=(2, 0))
        self._label(inner, note, 9, FAINT).pack(side="right", anchor="e")
        return root

    def _build_ui(self):
        base = tk.Frame(self, bg=BG)
        base.pack(fill="both", expand=True)
        sidebar = tk.Frame(base, bg=SIDEBAR, width=222, padx=17, pady=22)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        self._build_sidebar(sidebar)
        tk.Frame(base, width=1, bg=BORDER).pack(side="left", fill="y")

        main = tk.Frame(base, bg=BG, padx=21, pady=16)
        main.pack(side="left", fill="both", expand=True)
        topbar = tk.Frame(main, bg=BG)
        topbar.pack(fill="x", pady=(0, 12))
        self._label(topbar, "STUDIO  /  BIBLIOTHÈQUE AUDIO", 10, WHITE, True).pack(side="left")
        self._label(topbar, "●  APPLICATION LOCALE", 9, MINT, True).pack(side="right")

        banner = tk.Frame(main, bg=BORDER, padx=1, pady=1)
        banner.pack(fill="x", pady=(0, 12))
        hero = tk.Canvas(banner, bg="#1C2143", height=166, highlightthickness=0, bd=0)
        hero.pack(fill="x")
        cover = ASSETS / "studio_banner.png"
        if cover.exists():
            try:
                self._banner = tk.PhotoImage(file=str(cover))
                hero.create_image(0, 0, anchor="nw", image=self._banner)
            except tk.TclError:
                pass
        hero.create_text(30, 30, anchor="w", text="WAVEBOX   •   EDITION VINYLE",
                         font=(FONT, 9, "bold"), fill="#B7C5FF")
        hero.create_text(30, 77, anchor="w", text="Ta musique. Ton ordre.",
                         font=(FONT, 27, "bold"), fill=WHITE)
        hero.create_text(30, 121, anchor="w", text="Des pistes propres, numérotées et prêtes à classer.",
                         font=(FONT, 11), fill="#C3CBE4")
        hero.create_text(30, 157, anchor="w", text="MP3 320 KB/S     ·     TITRES PERSONNALISÉS     ·     SANS COMPTE",
                         font=(FONT, 9, "bold"), fill="#6DE8D5")

        stats = tk.Frame(main, bg=BG)
        stats.pack(fill="x", pady=(0, 13))
        self._stat(stats, self.count_var, "DANS LA SÉLECTION", "01 → 50", MINT)
        self._stat(stats, "320 kb/s", "QUALITÉ SÉLECTIONNABLE", "MP3 / STÉRÉO", "#BCA4FF")
        end = tk.Frame(stats, bg=BORDER, padx=1, pady=1)
        end.pack(side="left", fill="both", expand=True)
        inner = tk.Frame(end, bg=PANEL_2, padx=16, pady=11)
        inner.pack(fill="both", expand=True)
        self._label(inner, "Local & privé", 17, "#FFD5AD", True).pack(anchor="w")
        self._label(inner, "TES FICHIERS, TON PC", 9, WHITE, True).pack(anchor="w", pady=(2, 0))

        core = tk.Frame(main, bg=BG)
        core.pack(fill="both", expand=True)
        core.grid_columnconfigure(0, weight=6, uniform="workspace")
        core.grid_columnconfigure(1, weight=5, uniform="workspace")
        core.grid_rowconfigure(0, weight=1)
        left_frame, left = self._card(core, padx=17, pady=15)
        left_frame.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        right_frame, right = self._card(core, padx=17, pady=15)
        right_frame.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        self._build_left(left)
        self._build_right(right)
        footer = tk.Frame(main, bg=BG)
        footer.pack(fill="x", pady=(10, 0))
        self._label(footer, "◉  MP3 44,1 kHz · Stéréo · Validation audio", 9, FAINT).pack(side="left")
        self._label(footer, "Pour les contenus dont le téléchargement est autorisé", 9, FAINT).pack(side="right")

    def _build_sidebar(self, parent):
        brand = tk.Frame(parent, bg=SIDEBAR)
        brand.pack(fill="x", pady=(1, 29))
        if self._icon is not None:
            self._small_icon = self._icon.subsample(10, 10)
            tk.Label(brand, image=self._small_icon, bg=SIDEBAR).pack(side="left", padx=(0, 10))
        title = tk.Frame(brand, bg=SIDEBAR)
        title.pack(side="left")
        self._label(title, "WAVEBOX", 15, WHITE, True).pack(anchor="w")
        self._label(title, "STUDIO  4.0", 9, MINT, True).pack(anchor="w", pady=(1, 0))
        self._label(parent, "ESPACE DE TRAVAIL", 9, FAINT, True).pack(anchor="w", pady=(0, 11))
        self.tab_download = self._button(parent, "↓   Télécharger des pistes", lambda: self._set_mode("download"))
        self.tab_download.pack(fill="x", pady=(0, 7))
        self.tab_organize = self._button(parent, "♫   Organiser le vinyle", lambda: self._set_mode("organize"))
        self.tab_organize.pack(fill="x")

        tk.Frame(parent, bg=BORDER, height=1).pack(fill="x", pady=(26, 22))
        self._label(parent, "TON PROJET", 9, FAINT, True).pack(anchor="w", pady=(0, 10))
        note = tk.Frame(parent, bg="#1A2340", padx=12, pady=13)
        note.pack(fill="x")
        self._label(note, "◉  LE VINYLE", 10, WHITE, True).pack(anchor="w")
        self._label(note, "01 · Colle les liens\n02 · Garde l'ordre\n03 · Prépare les faces",
                    10, MUTED, justify="left").pack(anchor="w", pady=(10, 0))

        bottom = tk.Frame(parent, bg=SIDEBAR)
        bottom.pack(side="bottom", fill="x")
        tk.Frame(bottom, bg=BORDER, height=1).pack(fill="x", pady=(0, 14))
        self._label(bottom, "Aucun compte requis", 9, MINT, True).pack(anchor="w")
        self._label(bottom, "Source ouverte · Python", 9, FAINT).pack(anchor="w", pady=(4, 0))

    def _build_left(self, parent):
        heading = tk.Frame(parent, bg=PANEL)
        heading.pack(fill="x", pady=(0, 9))
        self.mode_title = self._label(heading, "Ajouter des pistes", 15, WHITE, True)
        self.mode_title.pack(side="left")
        self.mode_step = self._label(heading, "01 / SOURCE", 9, MINT, True)
        self.mode_step.pack(side="right")
        self.mode_hint = self._label(parent, "Colle tes URL dans l'ordre voulu sur le disque.", 9, MUTED)
        self.mode_hint.pack(anchor="w", pady=(0, 6))

        self.mode_viewport = tk.Frame(parent, bg=PANEL)
        self.mode_viewport.pack(fill="both", expand=True)
        self._mode_canvas = tk.Canvas(self.mode_viewport, bg=PANEL, bd=0, highlightthickness=0)
        self._mode_canvas.pack(side="left", fill="both", expand=True)
        self._mode_scrollbar = ttk.Scrollbar(self.mode_viewport, orient="vertical", command=self._mode_canvas.yview)
        self._mode_scrollbar.pack(side="right", fill="y")
        self._mode_canvas.configure(yscrollcommand=self._mode_scrollbar.set)
        self.mode_slot = tk.Frame(self._mode_canvas, bg=PANEL)
        self._mode_window = self._mode_canvas.create_window((0, 0), window=self.mode_slot, anchor="nw")
        self.mode_slot.bind("<Configure>", lambda _: self._mode_canvas.configure(
            scrollregion=self._mode_canvas.bbox("all")))
        self._mode_canvas.bind("<Configure>", lambda event: self._mode_canvas.itemconfigure(
            self._mode_window, width=event.width))
        self.download_panel = tk.Frame(self.mode_slot, bg=PANEL)
        self._setting_label(self.download_panel, "LIENS VIDÉO   ·   UN LIEN PAR LIGNE")
        link_frame = tk.Frame(self.download_panel, bg=BORDER, padx=1, pady=1)
        link_frame.pack(fill="both", expand=True)
        self.links = tk.Text(link_frame, height=3, bg=FIELD, fg=WHITE,
                             insertbackground=MINT, relief="flat", bd=0,
                             padx=12, pady=10, font=("Consolas", 10),
                             undo=True, wrap="none", selectbackground="#443F74")
        self.links.pack(fill="both", expand=True)
        self.links.bind("<KeyRelease>", lambda _: self._refresh_count())
        self.links.bind("<<Paste>>", lambda _: self.after(100, self._refresh_count))
        toolbar = tk.Frame(self.download_panel, bg=PANEL)
        toolbar.pack(fill="x", pady=(5, 7))
        self._button(toolbar, "⎘  Coller les liens", self._paste).pack(side="left")
        self._button(toolbar, "Effacer", self._clear_links).pack(side="left", padx=(8, 0))
        self._label(toolbar, "Tri : ordre de collage", 9, MINT, True).pack(side="right")

        titlebar = tk.Frame(self.download_panel, bg=PANEL)
        titlebar.pack(fill="x", pady=(0, 5))
        self._label(titlebar, "NOMS DES PISTES   ·   FACULTATIF", 9, MUTED, True).pack(side="left")
        tk.Button(titlebar, text="Charger mes 12 titres", command=self._fill_vinyl_titles,
                  bg=PANEL, fg=MINT, activebackground=PANEL, activeforeground=WHITE,
                  font=(FONT, 9, "bold"), relief="flat", bd=0, cursor="hand2").pack(side="right")
        self.custom_titles = tk.Text(self.download_panel, height=2, bg=FIELD, fg=WHITE,
                                     insertbackground=MINT, relief="flat", bd=0,
                                     padx=12, pady=10, font=("Consolas", 10), wrap="none",
                                     highlightbackground=BORDER, highlightthickness=1)
        self.custom_titles.pack(fill="x")
        self._label(self.download_panel, "Un titre par lien. Exemple : 01 - Everly Brothers - Bye Bye Love.mp3",
                    9, FAINT).pack(anchor="w", pady=(7, 0))

        self.organize_panel = tk.Frame(self.mode_slot, bg=PANEL)
        self._setting_label(self.organize_panel, "TA PLAYLIST   ·   ORDRE DES PISTES")
        self._label(self.organize_panel, "Réorganise les fichiers déjà présents sur ton PC.\nLes originaux ne sont jamais modifiés.",
                    9, MUTED, justify="left").pack(anchor="w", pady=(0, 10))
        self._button(self.organize_panel, "+  Ajouter des MP3", self._choose_for_organize, primary=True).pack(anchor="w", pady=(0, 10))
        list_container = tk.Frame(self.organize_panel, bg=BORDER, padx=1, pady=1)
        list_container.pack(fill="both", expand=True)
        self.organize_list = tk.Listbox(list_container, bg=FIELD, fg=WHITE,
                                       font=(FONT, 10), relief="flat", bd=0,
                                       selectbackground="#3B4C7A", activestyle="none")
        self.organize_list.pack(fill="both", expand=True, padx=8, pady=7)
        buttons = tk.Frame(self.organize_panel, bg=PANEL)
        buttons.pack(fill="x", pady=(11, 0))
        self._button(buttons, "↑  Monter", lambda: self._move_organize(-1)).pack(side="left")
        self._button(buttons, "↓  Descendre", lambda: self._move_organize(1)).pack(side="left", padx=(7, 0))
        self._button(buttons, "Retirer", self._remove_organize).pack(side="left", padx=(7, 0))
        self._label(self.organize_panel, "Les copies seront nommées 01, 02, 03... sans recompression.",
                    9, MINT).pack(anchor="w", pady=(8, 0))

        settings = tk.Frame(parent, bg=PANEL)
        settings.pack(fill="x", pady=(12, 0), before=self.mode_viewport)
        tk.Frame(settings, bg=BORDER, height=1).pack(fill="x", pady=(0, 10))
        self.numbering_check = tk.Checkbutton(settings, text="Numérotation automatique  ·  01, 02, 03...",
                                               variable=self.numbering_var, command=self._remember,
                                               bg=PANEL, fg=MINT, activebackground=PANEL,
                                               activeforeground=MINT, selectcolor=FIELD,
                                               font=(FONT, 10, "bold"), anchor="w")
        self.numbering_check.pack(fill="x", pady=(0, 8))
        row = tk.Frame(settings, bg=PANEL)
        row.pack(fill="x", pady=(0, 8))
        quality_section = tk.Frame(row, bg=PANEL)
        quality_section.pack(side="left", fill="x", expand=True)
        self._setting_label(quality_section, "QUALITÉ")
        self.quality_menu = ttk.Combobox(quality_section, state="readonly", textvariable=self.quality_var,
                                         values=list(QUALITY_OPTIONS), style="Wave.TCombobox", font=(FONT, 10))
        self.quality_menu.pack(fill="x")
        self.quality_menu.bind("<<ComboboxSelected>>", lambda _: self._remember())
        self._label(settings, "DESTINATION DES FICHIERS", 9, MUTED, True).pack(anchor="w", pady=(2, 6))
        folder_line = tk.Frame(settings, bg=PANEL)
        folder_line.pack(fill="x")
        self.folder_entry = tk.Entry(folder_line, textvariable=self.folder_var, bg=FIELD, fg=WHITE,
                                     insertbackground=WHITE, relief="flat", bd=0,
                                     highlightthickness=1, highlightbackground=BORDER,
                                     font=(FONT, 9))
        self.folder_entry.pack(side="left", fill="x", expand=True, ipady=10)
        self._button(folder_line, "Choisir", self._select_folder).pack(side="left", padx=(7, 0))
        self._button(folder_line, "↗", self._open_folder).pack(side="left", padx=(6, 0))
        self.folder_entry.bind("<FocusOut>", lambda _: self._remember())
        self.folder_entry.bind("<Return>", lambda _: self._remember())
        self._set_mode("download")

    def _build_right(self, parent):
        heading = tk.Frame(parent, bg=PANEL)
        heading.pack(fill="x", pady=(0, 5))
        self._label(heading, "Activité du studio", 15, WHITE, True).pack(side="left")
        self._label(heading, "●  EN DIRECT", 9, MINT, True).pack(side="right")
        self._label(parent, "Suivi des fichiers audio, progression et résultat.",
                    9, MUTED).pack(anchor="w", pady=(0, 12))
        table_container = tk.Frame(parent, bg=BORDER, padx=1, pady=1)
        table_container.pack(fill="x")
        table_area = tk.Frame(table_container, bg=PANEL)
        table_area.pack(fill="both", expand=True)
        self.table = ttk.Treeview(table_area, columns=("title", "status", "size"), show="headings",
                                  style="Wave.Treeview", selectmode="browse", height=4)
        self.table.heading("title", text="PISTE")
        self.table.heading("status", text="ÉTAT")
        self.table.heading("size", text="TAILLE")
        self.table.column("title", minwidth=124, width=200, stretch=True)
        self.table.column("status", minwidth=82, width=98, stretch=False)
        self.table.column("size", minwidth=55, width=62, stretch=False, anchor="e")
        self.table.tag_configure("success", foreground=MINT)
        self.table.tag_configure("failed", foreground=RED)
        self.table.tag_configure("work", foreground=ORANGE)
        self.table.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_area, orient="vertical", command=self.table.yview)
        scrollbar.pack(side="right", fill="y")
        self.table.configure(yscrollcommand=scrollbar.set)

        barhead = tk.Frame(parent, bg=PANEL)
        barhead.pack(fill="x", pady=(15, 7))
        self._label(barhead, "PROGRESSION DU LOT", 9, MUTED, True).pack(side="left")
        tk.Label(barhead, textvariable=self.hero_progress, bg=PANEL, fg=MINT,
                 font=(FONT, 9, "bold")).pack(side="right")
        self.progress = ttk.Progressbar(parent, orient="horizontal", maximum=100,
                                        style="Wave.Horizontal.TProgressbar")
        self.progress.pack(fill="x")
        tk.Label(parent, textvariable=self.status_var, bg=PANEL, fg=MUTED,
                 font=(FONT, 9), justify="left", anchor="w", wraplength=340).pack(fill="x", pady=(7, 12))
        actions = tk.Frame(parent, bg=PANEL)
        actions.pack(fill="x")
        self.start_btn = self._button(actions, "↓  Télécharger les MP3", self._start, primary=True)
        self.start_btn.pack(side="left", fill="x", expand=True)
        self.stop_btn = self._button(actions, "Arrêter", self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))

        self._label(parent, "DERNIER FICHIER CRÉÉ", 9, MUTED, True).pack(anchor="w", pady=(16, 5))
        tk.Label(parent, textvariable=self.result_var, bg=PANEL, fg=MINT, anchor="w",
                 justify="left", wraplength=360, font=(FONT, 9)).pack(fill="x")
        utility = tk.Frame(parent, bg=PANEL)
        utility.pack(fill="x", pady=(8, 0))
        self._button(utility, "↗  Voir le fichier", self._reveal_file).pack(side="left", fill="x", expand=True)
        self._button(utility, "≡  Journal", self._open_log_dialog).pack(side="left", padx=(8, 0))
        # Mémoire locale, cachée : le journal s'ouvre dans une fenêtre dédiée.
        self.log_text = tk.Text(parent, height=3, bg=FIELD, fg=MUTED,
                                font=("Consolas", 9), relief="flat", state="disabled", wrap="word",
                                bd=0, padx=9, pady=9)

    def _open_log_dialog(self):
        window = tk.Toplevel(self)
        window.title("WaveBox Studio — Journal technique")
        window.geometry("760x420")
        window.minsize(450, 260)
        window.configure(bg=BG)
        self._label(window, "Journal des traitements", 15, WHITE, True).pack(anchor="w", padx=19, pady=(17, 7))
        self._label(window, "Ces messages restent sur ton ordinateur ; aucun envoi automatique.", 9, MUTED).pack(anchor="w", padx=19)
        holder = tk.Frame(window, bg=PANEL, padx=12, pady=12)
        holder.pack(fill="both", expand=True, padx=19, pady=15)
        readout = tk.Text(holder, bg=FIELD, fg=WHITE, font=("Consolas", 10),
                          relief="flat", padx=11, pady=11, wrap="word")
        readout.pack(fill="both", expand=True)
        readout.insert("1.0", self.log_text.get("1.0", "end"))
        readout.configure(state="disabled")

    def _set_mode(self, mode: str):
        if self.busy:
            return
        if mode not in ("download", "organize"):
            raise ValueError("Mode inconnu")
        self.mode = mode
        self.download_panel.pack_forget()
        self.organize_panel.pack_forget()
        if mode == "download":
            self.download_panel.pack(fill="both", expand=True)
            title, hint, action = ("Ajouter des pistes", "Colle tes URL dans l'ordre voulu sur le disque.",
                                   "↓  Télécharger les MP3")
        else:
            self.organize_panel.pack(fill="both", expand=True)
            title, hint, action = ("Organiser ton vinyle", "Classe tes fichiers MP3, sans toucher aux originaux.",
                                   "♫  Créer la sélection ordonnée")
        if mode == "organize":
            n = len(self.organize_files)
            self.count_var.set(f"{n:02d} piste" + ("s" if n != 1 else ""))
        else:
            n = len([x for x in self.links.get("1.0", "end").splitlines() if x.strip()])
            self.count_var.set(f"{n:02d} piste" + ("s" if n != 1 else ""))
        self._mode_canvas.yview_moveto(0)
        self.mode_title.configure(text=title)
        self.mode_step.configure(text="01 / SOURCE" if mode == "download" else "02 / PLAYLIST")
        self.mode_hint.configure(text=hint)
        for button, active in ((self.tab_download, mode == "download"),
                               (self.tab_organize, mode == "organize")):
            base = "#39305D" if active else PANEL_2
            button._normal_bg = base
            button._hover_bg = "#514276" if active else "#283750"
            button.configure(bg=base, fg=WHITE if active else MUTED)
        if hasattr(self, "start_btn"):
            self.start_btn.configure(text=action)

    def _refresh_count(self):
        n = len([x for x in self.links.get("1.0", "end").splitlines() if x.strip()])
        self.count_var.set(f"{n:02d} piste" + ("s" if n != 1 else ""))

    def _paste(self):
        try:
            self.links.insert("insert", self.clipboard_get())
            self._refresh_count()
        except tk.TclError:
            messagebox.showinfo("Presse-papiers", "Aucun texte trouvé dans le presse-papiers")

    def _fill_vinyl_titles(self):
        template = Path(__file__).resolve().parent / "TITRES_EXEMPLE_VINYLE.txt"
        try:
            titles = template.read_text(encoding="utf-8").strip()
        except OSError as exc:
            messagebox.showerror("Titres", f"Liste introuvable : {exc}")
            return
        self.custom_titles.delete("1.0", "end")
        self.custom_titles.insert("1.0", titles)

    def _clear_links(self):
        self.links.delete("1.0", "end")
        self._refresh_count()

    def _refresh_organize(self):
        self.organize_list.delete(0, "end")
        for i, path in enumerate(self.organize_files, 1):
            self.organize_list.insert("end", f"{i:02d}   {remove_track_prefix(path.stem)}")
        if self.mode == "organize":
            n = len(self.organize_files)
            self.count_var.set(f"{n:02d} piste" + ("s" if n != 1 else ""))

    def _choose_for_organize(self):
        paths = filedialog.askopenfilenames(title="Choisir les MP3 à classer pour le vinyle",
                                            filetypes=[("MP3", "*.mp3")])
        for raw in paths:
            path = Path(raw)
            if path not in self.organize_files and len(self.organize_files) < MAX_JOBS:
                self.organize_files.append(path)
        self._refresh_organize()

    def _move_organize(self, direction: int):
        selection = self.organize_list.curselection()
        if not selection:
            return
        start = selection[0]
        end = start + direction
        if not 0 <= end < len(self.organize_files):
            return
        self.organize_files[start], self.organize_files[end] = self.organize_files[end], self.organize_files[start]
        self._refresh_organize()
        self.organize_list.selection_set(end)
        self.organize_list.activate(end)

    def _remove_organize(self):
        for pos in reversed(self.organize_list.curselection()):
            del self.organize_files[pos]
        self._refresh_organize()

    def _select_folder(self):
        previous = Path(self.folder_var.get())
        chosen = filedialog.askdirectory(title="Choisir le dossier des MP3",
                                          initialdir=str(previous) if previous.is_dir() else str(Path.home()))
        if chosen:
            self.folder_var.set(chosen)
            self._remember()

    def _remember(self):
        prefs_save(self.folder_var.get(), self.quality_var.get(), self.numbering_var.get())

    def _open_folder(self):
        try:
            path = Path(self.folder_var.get()).expanduser()
            path.mkdir(parents=True, exist_ok=True)
            self._remember()
            if sys.platform == "win32":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            messagebox.showerror("Dossier", f"Impossible d'ouvrir le dossier : {exc}")

    def _reveal_file(self):
        if self.last_created is None or not self.last_created.is_file():
            messagebox.showinfo("Fichier", "Aucun MP3 récent à afficher")
            return
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(["explorer.exe", f"/select,{self.last_created}"], creationflags=flags)
        else:
            self._open_folder()

    def _start(self):
        if self.busy:
            return
        try:
            folder = Path(self.folder_var.get().strip()).expanduser()
            if not str(folder).strip() or not folder.is_absolute():
                raise ValueError("Choisis un dossier absolu de destination")
            folder.mkdir(parents=True, exist_ok=True)
            quality = QUALITY_OPTIONS[self.quality_var.get()]
            if self.mode == "download":
                raw = [s.strip() for s in self.links.get("1.0", "end").splitlines() if s.strip()]
                if not raw or len(raw) > MAX_JOBS:
                    raise ValueError("Colle entre 1 et 50 liens de vidéos")
                items = [parse_video_url(x) for x in raw]
                manual_names = [s.strip() for s in self.custom_titles.get("1.0", "end").splitlines() if s.strip()]
                if manual_names and len(manual_names) != len(items):
                    raise ValueError("Les titres personnalisés doivent correspondre au nombre de liens (1 titre par ligne).")
            else:
                items = list(self.organize_files)
                manual_names = []
                if not items:
                    raise ValueError("Ajoute au moins un MP3 à organiser")
                if any(not x.is_file() or x.suffix.lower() != ".mp3" for x in items):
                    raise ValueError("Un MP3 sélectionné n'existe plus")
        except (ValueError, OSError, KeyError) as exc:
            messagebox.showerror("Vérification", str(exc))
            return
        self._remember()
        self.busy = True
        self.stop_flag.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.tab_download.configure(state="disabled")
        self.tab_organize.configure(state="disabled")
        self.numbering_check.configure(state="disabled")
        self.quality_menu.configure(state="disabled")
        self.progress["value"] = 0
        for iid in self.table.get_children():
            self.table.delete(iid)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.result_var.set("Travail en cours…")
        for i, item in enumerate(items):
            name = str(item) if isinstance(item, str) else item.name
            if len(name) > 48:
                name = name[:45] + "…"
            self.table.insert("", "end", iid=f"job{i}", values=(name, "En attente", "—"))
        self.status_var.set(f"Préparation de {len(items)} piste(s)…")
        self.hero_progress.set("TRAITEMENT EN COURS")
        threading.Thread(target=self._worker, args=(self.mode, items, folder, quality, manual_names, self.numbering_var.get()), daemon=True).start()

    def _stop(self):
        self.stop_flag.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Annulation demandée…")
        self.hero_progress.set("ANNULATION…")

    def _on_close(self):
        if self.busy:
            if messagebox.askyesno("Fermer WaveBox", "Un traitement est en cours. L'arrêter avant de quitter ?"):
                self.pending_close = True
                self._stop()
            return
        self.destroy()

    def _emit(self, kind: str, *args):
        self.events.put((kind, *args))

    def _log(self, msg: str):
        self._emit("log", msg)

    def _read_events(self):
        try:
            while True:
                kind, *args = self.events.get_nowait()
                if kind == "status":
                    self.status_var.set(str(args[0]))
                elif kind == "progress":
                    self.progress["value"] = float(args[0])
                elif kind == "log":
                    self.log_text.configure(state="normal")
                    self.log_text.insert("end", str(args[0]) + "\n")
                    self.log_text.see("end")
                    self.log_text.configure(state="disabled")
                elif kind == "row":
                    iid = f"job{args[0]}"
                    if self.table.exists(iid):
                        self.table.item(iid, values=args[1:4], tags=(args[4],))
                elif kind == "created":
                    self.last_created = Path(args[0])
                    self.result_var.set(str(args[0]))
                elif kind == "done":
                    self.busy = False
                    good, bad, stopped = args
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.tab_download.configure(state="normal")
                    self.tab_organize.configure(state="normal")
                    self.numbering_check.configure(state="normal")
                    self.quality_menu.configure(state="readonly")
                    self.status_var.set(f"Terminé : {good} réussi(s), {bad} échec(s)" + (" · interrompu" if stopped else ""))
                    self.hero_progress.set("TERMINÉ" if not stopped else "INTERROMPU")
                    if good and not bad and not stopped:
                        self.progress["value"] = 100
                    if self.pending_close:
                        self.destroy()
                        return
        except queue.Empty:
            pass
        self.after(100, self._read_events)

    def _worker(self, mode, items, folder, quality, manual_names, numbering):
        ok = failed = 0
        stopped = False
        try:
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            if not Path(ffmpeg).is_file():
                raise RuntimeError("FFmpeg n'a pas été trouvé dans le paquet imageio-ffmpeg")
            for index, item in enumerate(items):
                if self.stop_flag.is_set():
                    stopped = True
                    break
                name = str(item) if isinstance(item, str) else item.name
                display = clean_name(name, 48)
                self._emit("row", index, display, "Traitement…", "—", "work")
                self._emit("progress", 0)
                self._emit("status", f"Fichier {index + 1}/{len(items)} : démarrage…")
                try:
                    if mode == "download":
                        override = manual_names[index] if manual_names else None
                        result = self._download(item, folder, quality, ffmpeg, index, len(items), override, numbering)
                    else:
                        source = Path(item)
                        title = numbered_name(index + 1, remove_track_prefix(source.stem), True)
                        self._emit("status", f"Copie sans réencodage : {title}")
                        result = copy_mp3_checked(source, folder, title, ffmpeg, self.stop_flag,
                                                  lambda s: self._emit("status", s))
                    self._emit("created", str(result))
                    self._emit("row", index, result.stem[:48], "MP3 vérifié", f"{result.stat().st_size / (1024*1024):.1f} Mo", "success")
                    self._log(f"✓ {result}")
                    ok += 1
                except Stopped:
                    stopped = True
                    self._log("Traitement arrêté à la demande de l'utilisateur")
                    self._emit("row", index, display, "Arrêté", "—", "failed")
                    break
                except Exception as exc:
                    failed += 1
                    self._emit("row", index, display, "Échec", "—", "failed")
                    self._log(f"✗ {display} : {str(exc)[:330]}")
                self._emit("progress", 100)
        except Exception as exc:
            failed += 1
            self._log(f"Erreur d'initialisation : {str(exc)[:350]}")
        finally:
            self._emit("done", ok, failed, stopped)

    def _download(self, url: str, folder: Path, bitrate: str, ffmpeg: str, index: int, total: int, override: str | None, numbering: bool) -> Path:
        with tempfile.TemporaryDirectory(prefix="wavebox_video_") as td:
            working = Path(td)
            throttle = [0.0]

            def progress(d):
                if self.stop_flag.is_set():
                    raise Stopped()
                if d.get("status") == "downloading":
                    now = time.monotonic()
                    if now - throttle[0] > 0.3:
                        throttle[0] = now
                        length = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                        size = d.get("downloaded_bytes") or 0
                        percent = min(100, 100 * size / length) if length else 0
                        self._emit("progress", percent * 0.76)
                        self._emit("status", f"Vidéo {index+1}/{total} : réception {percent:.0f} %")

            class NoisyLogger:
                def debug(self, msg):
                    pass
                def warning(self, msg):
                    pass
                def error(self, msg):
                    pass

            opts = {
                "format": "bestaudio/best",
                "outtmpl": str(working / "%(id)s.%(ext)s"),
                "paths": {"home": str(working)},
                "ffmpeg_location": ffmpeg,
                "noplaylist": True,
                "ignoreconfig": True,
                "quiet": True,
                "no_warnings": True,
                "logger": NoisyLogger(),
                "progress_hooks": [progress],
                "retries": 3,
                "fragment_retries": 3,
                "concurrent_fragment_downloads": 1,
                "windowsfilenames": True,
                "nocheckcertificate": False,
            }
            with yt_dlp.YoutubeDL(opts) as client:
                info = client.extract_info(url, download=True)
            if self.stop_flag.is_set():
                raise Stopped()
            if not info or info.get("entries"):
                raise RuntimeError("Aucune vidéo unique n'a pu être extraite")
            candidates = [p for p in working.iterdir() if p.is_file() and p.suffix.lower() not in
                          {".part", ".ytdl", ".json", ".jpg", ".png", ".webp"}]
            if not candidates:
                raise RuntimeError("Aucun flux audio trouvé. La vidéo est peut-être privée ou indisponible")
            source = max(candidates, key=lambda p: p.stat().st_size)
            raw_title = override or str(info.get("title") or "Audio")
            title = numbered_name(index + 1, raw_title, numbering)
            artist = str(info.get("artist") or info.get("creator") or info.get("uploader") or "")[:160]
            self._emit("status", f"Vidéo {index + 1}/{total} : conversion MP3…")
            self._emit("progress", 84)
            result = encode_and_validate(source, folder, title, artist, bitrate, ffmpeg,
                                         self.stop_flag, lambda s: self._emit("status", s))
            return result


if __name__ == "__main__":
    App().mainloop()
