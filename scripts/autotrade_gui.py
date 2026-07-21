#!/usr/bin/env python3
"""Local desktop GUI for AutoTrade -- start/stop/emergency-stop the shadow
loop and edit `.env` credentials without touching a terminal. Thin shell:
almost all logic lives in `autotrade.gui.control` / `autotrade.gui.env_file`;
this file only wires those up to customtkinter widgets (per this feature's
plan, widget code has no automated tests -- only the logic those two
modules hold does).

    python scripts/autotrade_gui.py
"""
from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import customtkinter

from autotrade.gui import control, env_file

ENV_KEY_ORDER = [
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "MT5_TERMINAL_PATH",
    "ANTHROPIC_API_KEY",
    "FINNHUB_API_KEY",
    "FMP_API_KEY",
    "EODHD_API_TOKEN",
    "RAPIDAPI_KEY",
    "ALPHAVANTAGE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
]

STATUS_POLL_MS = 1000
MASKED = "•"
START_CONFIRM_ATTEMPTS = 6
START_CONFIRM_INTERVAL_S = 0.5

customtkinter.set_appearance_mode("System")
customtkinter.set_default_color_theme("blue")


class ControlTab(customtkinter.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent")
        self.status_var = tk.StringVar(value="Loading status...")
        self._action_in_flight = False

        customtkinter.CTkLabel(
            self, textvariable=self.status_var, justify="left", anchor="w",
            font=customtkinter.CTkFont(family="Consolas", size=13),
        ).pack(anchor="w", fill="x", padx=15, pady=15)

        button_frame = customtkinter.CTkFrame(self, fg_color="transparent")
        button_frame.pack(anchor="w", padx=15, pady=15)

        # Explicit widths -- CTkButton's 140px default x4 plus padding overflows
        # the 640px window (the "Emergency Stop" label doesn't need the same
        # width as the shorter labels, so sizing them individually fits all
        # four inside the window instead of widening the window to fit the
        # default sizing).
        self.start_button = customtkinter.CTkButton(button_frame, text="Start", command=self._on_start, width=100)
        self.start_button.grid(row=0, column=0, padx=5)

        self.stop_button = customtkinter.CTkButton(button_frame, text="Stop", command=self._on_stop, width=100)
        self.stop_button.grid(row=0, column=1, padx=5)

        self.emergency_button = customtkinter.CTkButton(
            button_frame, text="Emergency Stop", command=self._on_emergency_stop, width=140,
            fg_color="#c0392b", hover_color="#922b21",
        )
        self.emergency_button.grid(row=0, column=2, padx=5)

        self.refresh_button = customtkinter.CTkButton(
            button_frame, text="Refresh", command=self.refresh_status, width=100,
        )
        self.refresh_button.grid(row=0, column=3, padx=5)

        self.refresh_status()
        self.after(STATUS_POLL_MS, self._poll_status)

    def _poll_status(self) -> None:
        self.refresh_status()
        self.after(STATUS_POLL_MS, self._poll_status)

    def refresh_status(self) -> None:
        report = control.build_status()
        self.status_var.set(control.format_status(report))
        self._sync_button_states(report)

    def _sync_button_states(self, report) -> None:
        # An action still in flight owns button state -- a periodic poll or a
        # just-finished action must never re-enable buttons out from under it.
        if self._action_in_flight:
            return
        self.start_button.configure(state="normal" if control.can_start(report) else "disabled")
        self.stop_button.configure(state="normal")
        self.emergency_button.configure(state="normal")

    def _run_action(self, action, message_fn) -> None:
        # Start/Stop/Emergency-Stop are treated as one mutually-exclusive unit
        # (not per-action button subsets) so an overlapping action can never
        # have its buttons re-enabled by a different action's completion.
        if self._action_in_flight:
            return
        self._action_in_flight = True
        for button in (self.start_button, self.stop_button, self.emergency_button):
            button.configure(state="disabled")

        def worker() -> None:
            result = action()
            self.after(0, lambda: self._on_action_done(result, message_fn))

        threading.Thread(target=worker, daemon=True).start()

    def _on_action_done(self, result, message_fn) -> None:
        self._action_in_flight = False
        self.refresh_status()

        success, message = message_fn(result)
        if success:
            messagebox.showinfo("AutoTrade", message)
        else:
            messagebox.showerror("AutoTrade", message)

    @staticmethod
    def _default_message(result) -> tuple[bool, str]:
        output = ((result.stdout or "") + (result.stderr or "")).strip()
        if result.returncode == 0:
            return True, output or "Done."
        return False, output or f"Failed (exit code {result.returncode})."

    @staticmethod
    def _start_and_wait_for_confirmation() -> tuple:
        result = control.start_bot()
        confirmed = False
        if result.returncode == 0:
            for _ in range(START_CONFIRM_ATTEMPTS):
                if control.build_status().loop_running:
                    confirmed = True
                    break
                time.sleep(START_CONFIRM_INTERVAL_S)
        return result, confirmed

    @classmethod
    def _start_message(cls, outcome) -> tuple[bool, str]:
        result, confirmed = outcome
        success, message = cls._default_message(result)
        if success and not confirmed:
            return True, (
                "Start requested, but the loop does not appear to be running yet -- "
                "check the console window that opened, or click Refresh in a moment."
            )
        return success, message

    def _on_start(self) -> None:
        self._run_action(self._start_and_wait_for_confirmation, self._start_message)

    def _on_stop(self) -> None:
        self._run_action(control.stop_bot, self._default_message)

    def _on_emergency_stop(self) -> None:
        if self._action_in_flight:
            return
        if not messagebox.askyesno(
            "Emergency Stop",
            "Close ALL open positions and halt trading? This cannot be undone.",
        ):
            return
        self._run_action(control.emergency_stop_bot, self._default_message)


class SettingsTab(customtkinter.CTkFrame):
    def __init__(self, parent):
        super().__init__(parent, fg_color="transparent")
        self._dirty = False

        env_file.ensure_env_exists()
        self._doc = env_file.parse_env(env_file.DEFAULT_ENV_PATH.read_text(encoding="utf-8"))

        self._vars: dict[str, tk.StringVar] = {}
        self._entries: dict[str, customtkinter.CTkEntry] = {}

        # Scrollable -- 12 rows don't fit the window's fixed height, and a
        # plain CTkFrame just clips the overflow with no way to reach the
        # rest (the bug this fixes: ALPHAVANTAGE_API_KEY/TELEGRAM_* were
        # silently unreachable, not just visually cut off).
        container = customtkinter.CTkScrollableFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=15, pady=15)
        container.columnconfigure(1, weight=1)

        for row, key in enumerate(ENV_KEY_ORDER):
            customtkinter.CTkLabel(container, text=key).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=3)

            var = tk.StringVar(value=self._doc.get(key) or "")
            var.trace_add("write", lambda *_args: self._mark_dirty())
            self._vars[key] = var

            entry = customtkinter.CTkEntry(
                container, textvariable=var, width=400, show=MASKED if key in env_file.SECRET_KEYS else "",
            )
            entry.grid(row=row, column=1, sticky="we", pady=3)
            self._entries[key] = entry

            if key in env_file.SECRET_KEYS:
                customtkinter.CTkButton(
                    container, text="Show", width=60, command=lambda k=key: self._toggle_visibility(k),
                ).grid(row=row, column=2, padx=(5, 0))

        customtkinter.CTkButton(self, text="Save", command=self._on_save).pack(anchor="w", padx=15, pady=15)

    def _toggle_visibility(self, key: str) -> None:
        entry = self._entries[key]
        entry.configure(show="" if entry.cget("show") else MASKED)

    def _mark_dirty(self) -> None:
        self._dirty = True

    def is_dirty(self) -> bool:
        return self._dirty

    def _on_save(self) -> None:
        changed = {
            key: self._vars[key].get()
            for key in ENV_KEY_ORDER
            if self._vars[key].get() != (self._doc.get(key) or "")
        }

        for key, value in changed.items():
            error = env_file.validate_field(key, value)
            if error is not None:
                messagebox.showerror("AutoTrade", f"{key}: {error}")
                return

        for key, value in changed.items():
            self._doc.set(key, value)
        env_file.write_env_atomic(env_file.DEFAULT_ENV_PATH, self._doc.render())
        self._dirty = False

        if control.build_status().loop_running:
            messagebox.showinfo("AutoTrade", "Saved. Changes take effect on next start.")
        else:
            messagebox.showinfo("AutoTrade", "Saved.")


def main() -> int:
    root = customtkinter.CTk()
    root.title("AutoTrade Control")
    root.geometry("640x420")

    tabview = customtkinter.CTkTabview(root)
    tabview.pack(fill="both", expand=True, padx=10, pady=10)

    tabview.add("Control")
    tabview.add("Settings (.env)")

    control_tab = ControlTab(tabview.tab("Control"))
    control_tab.pack(fill="both", expand=True)

    settings_tab = SettingsTab(tabview.tab("Settings (.env)"))
    settings_tab.pack(fill="both", expand=True)

    def on_close() -> None:
        if settings_tab.is_dirty() and not messagebox.askyesno("AutoTrade", "Discard unsaved .env changes?"):
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
