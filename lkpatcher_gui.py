#!/usr/bin/env python3
"""
lkpatcher-gui - a cross-platform (Windows/Linux) PySide6 frontend for lkpatcher.

Exposes every feature of the lkpatcher CLI as a native desktop application:

  * Load & inspect MediaTek LK bootloader images
  * Apply binary patches per category (fastboot, dm_verity, ...)
  * Patch security policies to disable verification
  * Re-sign modified images with the certificate bypass (override / wrap)
  * Partition management: list / info / dump / extract / add / replace /
    remove / add certificate
  * Configuration files (load / save / export defaults) and custom patch
    JSON files
  * Backups, dry-run and a live log with adjustable verbosity

Requirements:  Python >= 3.10, PySide6, lkpatcher (and its dependencies)

Usage:         python lkpatcher_gui.py

Author:        ILYESwsa (https://github.com/ILYESwsa)

SPDX-License-Identifier: GPL-3.0-or-later
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QSettings,
    QSize,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtGui import QAction, QFontDatabase, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

try:
    from lkpatcher import __version__ as LKPATCHER_VERSION
    from lkpatcher.cert_bypass import CertBypassMode
    from lkpatcher.config import LogLevel, PatcherConfig
    from lkpatcher.exceptions import LkPatcherError
    from lkpatcher.patcher import LkPatcher, PatchManager
    from lkpatcher.policy import analyze_lk_security_policies
except ImportError as exc:  # pragma: no cover - startup guard
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        _app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None,
            'lkpatcher GUI - missing dependency',
            'The lkpatcher library is not installed or not importable:\n\n'
            f'{exc}\n\n'
            'Install it with:\n'
            '    pip install git+https://github.com/R0rt1z2/lkpatcher\n\n'
            'Then start this GUI again.',
        )
    except Exception:  # noqa: BLE001 - last resort
        print(f'Failed to import lkpatcher: {exc}', file=sys.stderr)
    sys.exit(1)


APP_VERSION = '1.0.2'
ORG_NAME = 'lkpatcher-gui'
APP_NAME = 'lkpatcher-gui'

IMAGE_FILTER = 'Bootloader image (*.img *.bin);;All files (*)'
JSON_FILTER = 'JSON file (*.json);;All files (*)'
LOG_FILTER = 'Log file (*.log *.txt);;All files (*)'

CERT_MODES: list[tuple[str, str | None]] = [
    ('No cert bypass (patch only)', None),
    ('Cert bypass - override (default)', 'override'),
    ('Cert bypass - wrap', 'wrap'),
]

CATEGORY_DESCRIPTIONS = {
    'fastboot': 'Unlock fastboot access (force unlock-bit check to return 0)',
    'dm_verity': 'Disable the dm-verity / unlocked warning message',
    'orange_state': 'Disable the orange state (LCS) warning message',
    'red_state': 'Disable the red state (verification) warning message',
}


def human_size(num: float) -> str:
    """Return a human readable size string."""
    for unit in ('B', 'KB', 'MB', 'GB'):
        if num < 1024 or unit == 'GB':
            return f'{num:.0f} {unit}' if unit == 'B' else f'{num:.1f} {unit}'
        num /= 1024
    return f'{num:.1f} GB'


# ---------------------------------------------------------------------------
# Logging / threading plumbing
# ---------------------------------------------------------------------------
class LogEmitter(QObject):
    """QObject that forwards log records to the GUI thread via a signal."""

    message = Signal(str)


class QtLogHandler(logging.Handler):
    """logging.Handler that emits each record through a Qt signal."""

    def __init__(self, emitter: LogEmitter) -> None:
        super().__init__()
        self._emitter = emitter
        self.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))

    def emit(self, record: logging.LogRecord) -> None:  # noqa: A003
        try:
            self._emitter.message.emit(self.format(record))
        except Exception:  # noqa: BLE001 - never break logging
            self.handleError(record)


class WorkerSignals(QObject):
    """Signals exposed by a Worker."""

    done = Signal(object)
    fail = Signal(str)


class Worker(QRunnable):
    """Run a callable on the global thread pool and report back via signals."""

    def __init__(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - reported to the GUI
            logging.getLogger(__name__).debug('Worker failed', exc_info=True)
            self.signals.fail.emit(str(exc) or exc.__class__.__name__)
        else:
            self.signals.done.emit(result)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    """Main application window for the lkpatcher GUI."""

    def __init__(self) -> None:
        super().__init__()
        self.interactive = True  # False suppresses modal dialogs (tests)

        # --- state -------------------------------------------------------
        self.patcher: LkPatcher | None = None
        self.image_path: Path | None = None
        self._config = PatcherConfig()
        self._busy = False
        self._modified = False
        self._workers: set[Worker] = set()
        self.category_boxes: dict[str, QCheckBox] = {}
        self._categories_placeholder: QLabel | None = None

        self._build_ui()

        # --- logging -----------------------------------------------------
        self._log_emitter = LogEmitter()
        self._log_emitter.message.connect(self._append_log)
        self._log_handler = QtLogHandler(self._log_emitter)
        logging.getLogger().addHandler(self._log_handler)
        logging.getLogger().setLevel(logging.DEBUG)
        self._log_handler.setLevel(logging.INFO)

        self._restore_settings()
        self._update_enablements()
        self.log(
            f'lkpatcher GUI v{APP_VERSION} by ILYESwsa - frontend for '
            f'lkpatcher v{LKPATCHER_VERSION} by R0rt1z2'
        )

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self.setWindowTitle(
            f'lkpatcher GUI - MediaTek bootloader patcher '
            f'(lkpatcher v{LKPATCHER_VERSION})'
        )
        self.resize(1020, 720)
        self.setMinimumSize(QSize(860, 600))

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(8)

        root.addLayout(self._build_top_area())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_patch_tab(), 'Patch')
        self.tabs.addTab(self._build_partitions_tab(), 'Partitions')
        self.tabs.addTab(self._build_policies_tab(), 'Security policies')
        self.tabs.addTab(self._build_log_tab(), 'Log')
        root.addWidget(self.tabs, 1)

        self.setCentralWidget(central)
        self._build_menu()
        self._build_status_bar()

    def _build_top_area(self) -> QHBoxLayout:
        box = QHBoxLayout()
        form = QGridLayout()
        form.setHorizontalSpacing(6)
        form.setVerticalSpacing(4)

        form.addWidget(QLabel('Image:'), 0, 0, Qt.AlignmentFlag.AlignRight)
        self.image_edit = QLineEdit()
        self.image_edit.setPlaceholderText(
            'Path to the LK bootloader image (lk.img / bootloader.img ...)'
        )
        self.image_edit.returnPressed.connect(self._on_load_clicked)
        form.addWidget(self.image_edit, 0, 1)
        self.image_browse_btn = QPushButton('Browse...')
        self.image_browse_btn.clicked.connect(self._browse_image)
        form.addWidget(self.image_browse_btn, 0, 2)
        self.load_btn = QPushButton('Load')
        self.load_btn.setDefault(True)
        self.load_btn.clicked.connect(self._on_load_clicked)
        form.addWidget(self.load_btn, 0, 3)

        form.addWidget(QLabel('Output:'), 1, 0, Qt.AlignmentFlag.AlignRight)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText(
            'Output file (default: <image>-patched.img next to the source)'
        )
        form.addWidget(self.output_edit, 1, 1)
        self.output_browse_btn = QPushButton('Browse...')
        self.output_browse_btn.clicked.connect(self._browse_output)
        form.addWidget(self.output_browse_btn, 1, 2)
        # keep the grid aligned: empty cell under Load
        form.addWidget(QLabel(''), 1, 3)

        box.addLayout(form, 1)
        return box

    def _build_menu(self) -> None:
        menubar: QMenuBar = self.menuBar()

        file_menu: QMenu = menubar.addMenu('&File')
        open_action = QAction('&Open image...', self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._browse_image)
        file_menu.addAction(open_action)
        file_menu.addSeparator()
        quit_action = QAction('&Quit', self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu: QMenu = menubar.addMenu('&Help')
        about_action = QAction('&About lkpatcher GUI', self)
        about_action.triggered.connect(self._about)
        help_menu.addAction(about_action)

    def _build_status_bar(self) -> None:
        self.statusBar().showMessage('Ready - load an image to begin')
        self.status_image_lbl = QLabel('No image loaded')
        self.statusBar().addPermanentWidget(self.status_image_lbl)

    # --- Patch tab -----------------------------------------------------
    def _build_patch_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        # categories ------------------------------------------------------
        self.categories_group = QGroupBox('Patch categories')
        self.categories_layout = QVBoxLayout(self.categories_group)
        self.categories_hint = QLabel(
            'All categories are applied when every box is checked. '
            'Untick a box to exclude that category.'
        )
        self.categories_hint.setWordWrap(True)
        self.categories_layout.addWidget(self.categories_hint)
        self._rebuild_categories({})
        layout.addWidget(self.categories_group)

        # options ---------------------------------------------------------
        options = QGroupBox('Options')
        grid = QGridLayout(options)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)

        self.dry_run_cb = QCheckBox('Dry run (do not write any files)')
        self.dry_run_cb.setToolTip(
            'Simulate the run: report what would happen without saving.'
        )
        grid.addWidget(self.dry_run_cb, 0, 0)

        self.patch_policies_cb = QCheckBox(
            'Patch security policies (disable verification)'
        )
        self.patch_policies_cb.setToolTip(
            'Zeroes the LK security policy table and locks '
            'seccfg_get_lock_state() to LKS_LOCKED.'
        )
        grid.addWidget(self.patch_policies_cb, 0, 1)

        self.backup_cb = QCheckBox('Create backup before patching')
        grid.addWidget(self.backup_cb, 1, 0)

        backup_dir_row = QHBoxLayout()
        self.backup_dir_edit = QLineEdit()
        self.backup_dir_edit.setPlaceholderText('Backup directory (optional)')
        backup_dir_row.addWidget(self.backup_dir_edit)
        self.backup_dir_btn = QPushButton('Browse...')
        self.backup_dir_btn.clicked.connect(self._browse_backup_dir)
        backup_dir_row.addWidget(self.backup_dir_btn)
        grid.addLayout(backup_dir_row, 1, 1)

        self.verify_cb = QCheckBox('Verify patch format (hex validation)')
        self.verify_cb.setChecked(True)
        grid.addWidget(self.verify_cb, 2, 0)

        self.allow_incomplete_cb = QCheckBox(
            'Allow incomplete patching (continue when needles are missing)'
        )
        self.allow_incomplete_cb.setChecked(True)
        grid.addWidget(self.allow_incomplete_cb, 2, 1)
        layout.addWidget(options)

        # cert bypass -----------------------------------------------------
        cert_box = QGroupBox('Certificate bypass (patch + re-sign)')
        cert_layout = QHBoxLayout(cert_box)
        self.cert_combo = QComboBox()
        for label, mode in CERT_MODES:
            self.cert_combo.addItem(label, mode)
        self.cert_combo.currentIndexChanged.connect(self._on_cert_mode_changed)
        cert_layout.addWidget(self.cert_combo, 1)
        self.cert_hint_lbl = QLabel('')
        self.cert_hint_lbl.setWordWrap(True)
        cert_layout.addWidget(self.cert_hint_lbl, 2)
        layout.addWidget(cert_box)

        # custom patches ----------------------------------------------------
        patches_box = QGroupBox('Custom patches (JSON)')
        patches_layout = QHBoxLayout(patches_box)
        self.custom_patches_edit = QLineEdit()
        self.custom_patches_edit.setPlaceholderText(
            'Optional JSON file with additional patches (-j / --json-patches)'
        )
        self.custom_patches_edit.editingFinished.connect(
            self._on_custom_patches_changed
        )
        patches_layout.addWidget(self.custom_patches_edit, 1)
        self.custom_patches_btn = QPushButton('Browse...')
        self.custom_patches_btn.clicked.connect(self._browse_custom_patches)
        patches_layout.addWidget(self.custom_patches_btn)
        self.custom_patches_clear_btn = QPushButton('Clear')
        self.custom_patches_clear_btn.clicked.connect(self._clear_custom_patches)
        patches_layout.addWidget(self.custom_patches_clear_btn)
        layout.addWidget(patches_box)

        # configuration -----------------------------------------------------
        cfg_box = QGroupBox('Configuration')
        cfg_layout = QHBoxLayout(cfg_box)
        self.load_config_btn = QPushButton('Load config...')
        self.load_config_btn.clicked.connect(self._load_config_dialog)
        cfg_layout.addWidget(self.load_config_btn)
        self.save_config_btn = QPushButton('Save current settings...')
        self.save_config_btn.clicked.connect(self._save_config_dialog)
        cfg_layout.addWidget(self.save_config_btn)
        self.export_config_btn = QPushButton('Export default config...')
        self.export_config_btn.clicked.connect(self._export_config_dialog)
        cfg_layout.addWidget(self.export_config_btn)
        cfg_layout.addStretch(1)
        layout.addWidget(cfg_box)

        # run ---------------------------------------------------------------
        self.run_btn = QPushButton('Patch image')
        self.run_btn.setMinimumHeight(42)
        self.run_btn.clicked.connect(self._run_patch)
        layout.addWidget(self.run_btn)

        layout.addStretch(1)
        return tab

    # --- Partitions tab -------------------------------------------------
    def _build_partitions_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        # table -----------------------------------------------------------
        table_box = QGroupBox('Partitions in image')
        table_layout = QVBoxLayout(table_box)

        self.part_table = QTableWidget(0, 5)
        self.part_table.setHorizontalHeaderLabels(
            ['Name', 'Size', 'Memory address', 'Ext. header', 'Certificates']
        )
        self.part_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.part_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.part_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.part_table.verticalHeader().setVisible(False)
        header = self.part_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            header.setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents
            )
        self.part_table.itemDoubleClicked.connect(
            lambda _item: self._partition_info()
        )
        table_layout.addWidget(self.part_table)

        btn_row = QHBoxLayout()
        self.refresh_btn = QPushButton('Refresh')
        self.refresh_btn.clicked.connect(self.refresh_partitions)
        btn_row.addWidget(self.refresh_btn)
        self.info_btn = QPushButton('Info')
        self.info_btn.setToolTip(
            'Show detailed information (also via double-click)'
        )
        self.info_btn.clicked.connect(self._partition_info)
        btn_row.addWidget(self.info_btn)
        self.dump_btn = QPushButton('Dump partition...')
        self.dump_btn.clicked.connect(self._dump_partition_dialog)
        btn_row.addWidget(self.dump_btn)
        self.extract_btn = QPushButton('Extract all...')
        self.extract_btn.clicked.connect(self._extract_all_dialog)
        btn_row.addWidget(self.extract_btn)
        btn_row.addStretch(1)
        table_layout.addLayout(btn_row)
        layout.addWidget(table_box, 2)

        # details -----------------------------------------------------------
        details_box = QGroupBox('Details')
        details_layout = QVBoxLayout(details_box)
        self.details_view = QPlainTextEdit()
        self.details_view.setReadOnly(True)
        self.details_view.setMaximumHeight(140)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.details_view.setFont(font)
        self.details_view.setPlaceholderText(
            'Select a partition and press Info (or double-click it).'
        )
        details_layout.addWidget(self.details_view)
        layout.addWidget(details_box)

        # modify ------------------------------------------------------------
        modify_box = QGroupBox('Modify partitions (changes are kept in memory '
                               'until you save)')
        modify_grid = QGridLayout(modify_box)
        modify_grid.setHorizontalSpacing(8)
        modify_grid.setVerticalSpacing(6)

        # row: add
        modify_grid.addWidget(QLabel('Add:'), 0, 0)
        self.add_name_edit = QLineEdit()
        self.add_name_edit.setPlaceholderText('name')
        self.add_name_edit.setMaximumWidth(110)
        modify_grid.addWidget(self.add_name_edit, 0, 1)
        self.add_file_edit = QLineEdit()
        self.add_file_edit.setPlaceholderText('data file')
        modify_grid.addWidget(self.add_file_edit, 0, 2)
        self.add_file_btn = QPushButton('...')
        self.add_file_btn.setFixedWidth(34)
        self.add_file_btn.clicked.connect(
            lambda: self._browse_input_file(self.add_file_edit)
        )
        modify_grid.addWidget(self.add_file_btn, 0, 3)
        self.add_addr_edit = QLineEdit()
        self.add_addr_edit.setPlaceholderText('address (hex/dec)')
        self.add_addr_edit.setMaximumWidth(130)
        modify_grid.addWidget(self.add_addr_edit, 0, 4)
        self.add_legacy_cb = QCheckBox('Legacy header')
        self.add_legacy_cb.setToolTip(
            'Create the partition with the legacy (non-extended) header '
            'format (--partition-legacy).'
        )
        modify_grid.addWidget(self.add_legacy_cb, 0, 5)
        self.add_part_btn = QPushButton('Add partition')
        self.add_part_btn.clicked.connect(self._add_partition)
        modify_grid.addWidget(self.add_part_btn, 0, 6)

        # row: replace
        modify_grid.addWidget(QLabel('Replace:'), 1, 0)
        self.replace_combo = QComboBox()
        self.replace_combo.setMinimumWidth(110)
        modify_grid.addWidget(self.replace_combo, 1, 1)
        self.replace_file_edit = QLineEdit()
        self.replace_file_edit.setPlaceholderText('new data file')
        modify_grid.addWidget(self.replace_file_edit, 1, 2)
        self.replace_file_btn = QPushButton('...')
        self.replace_file_btn.setFixedWidth(34)
        self.replace_file_btn.clicked.connect(
            lambda: self._browse_input_file(self.replace_file_edit)
        )
        modify_grid.addWidget(self.replace_file_btn, 1, 3)
        self.replace_addr_edit = QLineEdit()
        self.replace_addr_edit.setPlaceholderText('address (hex/dec)')
        self.replace_addr_edit.setMaximumWidth(150)
        self.replace_addr_edit.setMinimumWidth(120)
        modify_grid.addWidget(self.replace_addr_edit, 1, 4)
        self.replace_btn = QPushButton('Replace partition')
        self.replace_btn.clicked.connect(self._replace_partition)
        modify_grid.addWidget(self.replace_btn, 1, 6)

        # row: remove
        modify_grid.addWidget(QLabel('Remove:'), 2, 0)
        self.remove_combo = QComboBox()
        self.remove_combo.setMinimumWidth(110)
        modify_grid.addWidget(self.remove_combo, 2, 1)
        self.remove_btn = QPushButton('Remove partition')
        self.remove_btn.clicked.connect(self._remove_partition)
        modify_grid.addWidget(self.remove_btn, 2, 6)

        # row: certificate
        modify_grid.addWidget(QLabel('Certificate:'), 3, 0)
        self.cert_part_combo = QComboBox()
        self.cert_part_combo.setMinimumWidth(110)
        modify_grid.addWidget(self.cert_part_combo, 3, 1)
        self.cert_file_edit = QLineEdit()
        self.cert_file_edit.setPlaceholderText('certificate file')
        modify_grid.addWidget(self.cert_file_edit, 3, 2)
        self.cert_file_btn = QPushButton('...')
        self.cert_file_btn.setFixedWidth(34)
        self.cert_file_btn.clicked.connect(
            lambda: self._browse_input_file(self.cert_file_edit)
        )
        modify_grid.addWidget(self.cert_file_btn, 3, 3)
        self.cert_type_combo = QComboBox()
        self.cert_type_combo.addItems(['cert1', 'cert2'])
        modify_grid.addWidget(self.cert_type_combo, 3, 4)
        self.add_cert_btn = QPushButton('Add certificate')
        self.add_cert_btn.clicked.connect(self._add_certificate)
        modify_grid.addWidget(self.add_cert_btn, 3, 6)

        modify_grid.setColumnStretch(2, 1)
        layout.addWidget(modify_box)

        # save ---------------------------------------------------------------
        self.save_modified_btn = QPushButton('Save modified image...')
        self.save_modified_btn.setToolTip(
            'Write the in-memory image (with all modifications) to the '
            'output file selected at the top.'
        )
        self.save_modified_btn.clicked.connect(self._save_modified)
        layout.addWidget(self.save_modified_btn)

        return tab

    # --- Policies tab ----------------------------------------------------
    def _build_policies_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self.analyze_btn = QPushButton('Analyze security policies')
        self.analyze_btn.clicked.connect(self._analyze_policies)
        row.addWidget(self.analyze_btn)
        self.goto_patch_btn = QPushButton('Patch policies...')
        self.goto_patch_btn.setToolTip(
            'Switch to the Patch tab and enable policy patching'
        )
        self.goto_patch_btn.clicked.connect(self._goto_patch_policies)
        row.addWidget(self.goto_patch_btn)
        row.addStretch(1)
        layout.addLayout(row)

        self.policy_info_lbl = QLabel('No analysis yet.')
        self.policy_info_lbl.setWordWrap(True)
        layout.addWidget(self.policy_info_lbl)

        self.policy_table = QTableWidget(0, 5)
        self.policy_table.setHorizontalHeaderLabels(
            [
                'Policy name',
                'nosbc + locked',
                'nosbc + unlocked',
                'sbc + locked',
                'sbc + unlocked',
            ]
        )
        self.policy_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.policy_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.policy_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.policy_table.verticalHeader().setVisible(False)
        pheader = self.policy_table.horizontalHeader()
        pheader.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 5):
            pheader.setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents
            )
        layout.addWidget(self.policy_table, 1)

        hint = QLabel(
            'Policy values are bitmasks: 0 means the action is disabled, '
            'non-zero values enable verification / download restrictions. '
            '"Patch policies" sets every policy to 0.'
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return tab

    # --- Log tab -----------------------------------------------------------
    def _build_log_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel('Level:'))
        self.log_level_combo = QComboBox()
        self.log_level_combo.addItems(
            ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']
        )
        self.log_level_combo.setCurrentText('INFO')
        self.log_level_combo.currentTextChanged.connect(
            self._on_log_level_changed
        )
        row.addWidget(self.log_level_combo)
        self.autoscroll_cb = QCheckBox('Auto-scroll')
        self.autoscroll_cb.setChecked(True)
        row.addWidget(self.autoscroll_cb)
        row.addStretch(1)
        self.save_log_btn = QPushButton('Save log...')
        self.save_log_btn.clicked.connect(self._save_log)
        row.addWidget(self.save_log_btn)
        self.clear_log_btn = QPushButton('Clear')
        self.clear_log_btn.clicked.connect(self._clear_log)
        row.addWidget(self.clear_log_btn)
        layout.addLayout(row)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.log_view.setFont(font)
        self.log_view.setPlaceholderText('Log output appears here...')
        layout.addWidget(self.log_view, 1)
        return tab

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def _restore_settings(self) -> None:
        settings = QSettings()
        geometry = settings.value('window/geometry')
        if geometry is not None:
            self.restoreGeometry(geometry)
        state = settings.value('window/state')
        if state is not None:
            self.restoreState(state)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt naming)
        if self._busy:
            self._warn('An operation is still running - please wait for it '
                       'to finish before closing.')
            event.ignore()
            return
        settings = QSettings()
        settings.setValue('window/geometry', self.saveGeometry())
        settings.setValue('window/state', self.saveState())
        logging.getLogger().removeHandler(self._log_handler)
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def log(self, message: str) -> None:
        """Append a plain GUI message (not via the logging module)."""
        self._append_log(message)

    def _append_log(self, message: str) -> None:
        if not message.strip():
            return
        self.log_view.appendPlainText(message)
        if self.autoscroll_cb.isChecked():
            scrollbar = self.log_view.verticalScrollBar()
            scrollbar.setValue(scrollbar.maximum())

    def _status(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)

    def _info(self, title: str, text: str) -> None:
        if self.interactive:
            QMessageBox.information(self, title, text)
        else:
            self.log(f'{title}: {text}')

    def _warn(self, text: str) -> None:
        if self.interactive:
            QMessageBox.warning(self, 'lkpatcher GUI', text)
        else:
            self.log(f'[WARNING] {text}')

    def _error(self, text: str) -> None:
        if self.interactive:
            QMessageBox.critical(self, 'lkpatcher GUI - error', text)
        else:
            self.log(f'[ERROR] {text}')

    def _confirm(self, text: str) -> bool:
        if not self.interactive:
            return True
        answer = QMessageBox.question(
            self,
            'lkpatcher GUI - confirm',
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _set_busy(self, busy: bool, text: str = '') -> None:
        self._busy = busy
        self.statusBar().showMessage('Working...' if busy else 'Ready')
        if text:
            self._append_log(text)
        self._update_enablements()

    def _update_enablements(self) -> None:
        loaded = self.patcher is not None
        busy = self._busy
        selected = (
            self.part_table.currentRow() >= 0
            if hasattr(self, 'part_table')
            else False
        )

        self.load_btn.setEnabled(not busy)
        self.image_browse_btn.setEnabled(not busy)
        self.run_btn.setEnabled(loaded and not busy)
        self.refresh_btn.setEnabled(loaded and not busy)
        self.info_btn.setEnabled(loaded and not busy and selected)
        self.dump_btn.setEnabled(loaded and not busy and selected)
        self.extract_btn.setEnabled(loaded and not busy)
        self.add_part_btn.setEnabled(loaded and not busy)
        self.replace_btn.setEnabled(loaded and not busy)
        self.remove_btn.setEnabled(loaded and not busy)
        self.add_cert_btn.setEnabled(loaded and not busy)
        self.save_modified_btn.setEnabled(loaded and not busy)
        self.analyze_btn.setEnabled(loaded and not busy)

    def _worker(
        self,
        fn: Callable[..., Any],
        on_done: Callable[[Any], None],
        on_fail: Callable[[str], None] | None = None,
    ) -> None:
        """Run fn on the thread pool; exactly one of on_done/on_fail fires."""
        worker = Worker(fn)
        self._workers.add(worker)

        def handle_done(payload: object) -> None:
            self._workers.discard(worker)
            self._set_busy(False)
            on_done(payload)

        def handle_fail(message: str) -> None:
            self._workers.discard(worker)
            self._set_busy(False)
            if on_fail is not None:
                on_fail(message)
            else:
                self._error(message)

        worker.signals.done.connect(handle_done)
        worker.signals.fail.connect(handle_fail)
        self._set_busy(True)
        QThreadPool.globalInstance().start(worker)

    # ------------------------------------------------------------------
    # File dialogs
    # ------------------------------------------------------------------
    def _browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open bootloader image', '', IMAGE_FILTER
        )
        if path:
            self.image_edit.setText(path)
            self.load_image_path(Path(path))

    def _browse_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, 'Select output file', self.output_edit.text().strip(),
            IMAGE_FILTER,
        )
        if path:
            self.output_edit.setText(path)

    def _browse_backup_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, 'Select backup directory',
            self.backup_dir_edit.text().strip(),
        )
        if path:
            self.backup_dir_edit.setText(path)

    def _browse_input_file(self, edit: QLineEdit) -> None:
        path, _ = QFileDialog.getOpenFileName(self, 'Select file', '', '')
        if path:
            edit.setText(path)

    def _browse_custom_patches(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Select custom patches file', '', JSON_FILTER
        )
        if path:
            self.custom_patches_edit.setText(path)
            self._on_custom_patches_changed()

    def _clear_custom_patches(self) -> None:
        self.custom_patches_edit.clear()
        self._on_custom_patches_changed()

    # ------------------------------------------------------------------
    # Image loading
    # ------------------------------------------------------------------
    def _on_load_clicked(self) -> None:
        text = self.image_edit.text().strip()
        if not text:
            self._browse_image()
            return
        self.load_image_path(Path(text))

    def _custom_patches_path(self) -> Path | None:
        text = self.custom_patches_edit.text().strip()
        if not text:
            return None
        path = Path(text)
        if not path.is_file():
            self._warn(f'Custom patches file not found: {path}')
            return None
        return path

    def load_image_path(self, path: Path) -> None:
        """Load an LK image in a background worker."""
        if self._busy:
            return
        if not path.is_file():
            self._error(f'Image file not found: {path}')
            return

        patches_file = self._custom_patches_path()

        def job() -> LkPatcher:
            config = PatcherConfig()
            return LkPatcher(path, patches_file, config=config)

        self._worker(
            job,
            on_done=lambda patcher: self._on_image_loaded(patcher, path),
            on_fail=lambda msg: self._error(
                f'Failed to load {path.name}:\n{msg}\n\n'
                'Make sure this is a valid MediaTek LK bootloader image.'
            ),
        )

    def _on_image_loaded(self, patcher: LkPatcher, path: Path) -> None:
        self.patcher = patcher
        self.image_path = path
        self._config = patcher.config
        self._modified = False

        analysis = patcher.analyze_image()
        self._rebuild_categories(patcher.patch_manager.patches)

        default_output = path.with_name(f'{path.stem}-patched{path.suffix or ".img"}')
        self.output_edit.setText(str(default_output))

        summary = (
            f'{path.name}  -  LK v{analysis["image_version"]}  -  '
            f'{analysis["partition_count"]} partitions  -  '
            f'{human_size(analysis["image_size"])}'
        )
        self.status_image_lbl.setText(f'{path.name} (not modified)')
        self.log(f'Loaded image: {summary}')
        self._status(f'Loaded {path.name}')

        self.refresh_partitions()
        self.policy_table.setRowCount(0)
        self.policy_info_lbl.setText('No analysis yet - press '
                                     '"Analyze security policies".')
        self.details_view.clear()
        self._update_enablements()

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------
    def _rebuild_categories(self, patches: dict[str, dict[str, str]]) -> None:
        """(Re)create the category checkboxes, preserving check states."""
        previous = {
            name: cb.isChecked() for name, cb in self.category_boxes.items()
        }
        for cb in self.category_boxes.values():
            self.categories_layout.removeWidget(cb)
            cb.deleteLater()
        self.category_boxes.clear()
        if self._categories_placeholder is not None:
            self.categories_layout.removeWidget(self._categories_placeholder)
            self._categories_placeholder.deleteLater()
            self._categories_placeholder = None

        if not patches:
            placeholder = QLabel('No patches loaded - defaults apply after '
                                 'loading an image.')
            self.categories_layout.addWidget(placeholder)
            self._categories_placeholder = placeholder
            return

        for name, needles in patches.items():
            cb = QCheckBox(f'{name}  ({len(needles)} patch'
                           f'{"es" if len(needles) != 1 else ""})')
            description = CATEGORY_DESCRIPTIONS.get(name, 'Custom category')
            tooltip = f'{description}\n\nNeedles:\n' + '\n'.join(
                f'  {needle} -> {patch}' for needle, patch in needles.items()
            )
            cb.setToolTip(tooltip)
            cb.setChecked(previous.get(name, True))
            self.categories_layout.addWidget(cb)
            self.category_boxes[name] = cb

    def _selected_categories(self) -> set[str]:
        return {
            name
            for name, cb in self.category_boxes.items()
            if cb.isChecked()
        }

    def _on_custom_patches_changed(self) -> None:
        """Reload patch definitions when the custom patches file changes."""
        patches_file = self._custom_patches_path()
        try:
            manager = PatchManager(patches_file)
        except LkPatcherError as exc:
            self._error(f'Failed to load custom patches:\n{exc}')
            return

        self._rebuild_categories(manager.patches)
        if self.patcher is not None:
            # give the live patcher the new patch set
            self.patcher.patch_manager = manager
            self.log(
                f'Loaded {manager.patches_count} patches in '
                f'{len(manager.patches)} categories from '
                f'{patches_file.name if patches_file else "defaults"}'
            )
        else:
            self.log(
                f'Custom patches set ({manager.patches_count} patches in '
                f'{len(manager.patches)} categories); load an image to use them.'
            )

    # ------------------------------------------------------------------
    # Config <-> widgets
    # ------------------------------------------------------------------
    def _sync_config(self, strict: bool = False) -> PatcherConfig:
        """Update the shared PatcherConfig in place from the widgets."""
        cfg = self._config
        cfg.log_level = LogLevel.from_string(
            self.log_level_combo.currentText()
        )
        cfg.backup = self.backup_cb.isChecked()
        backup_dir = self.backup_dir_edit.text().strip()
        cfg.backup_dir = Path(backup_dir) if backup_dir else None
        cfg.verify_patch = self.verify_cb.isChecked()
        cfg.allow_incomplete = self.allow_incomplete_cb.isChecked()
        cfg.dry_run = self.dry_run_cb.isChecked()

        selected = self._selected_categories()
        if not self.category_boxes or selected == set(self.category_boxes):
            cfg.patch_categories = set()  # empty == apply all
        else:
            if strict and not selected:
                raise ValueError(
                    'Select at least one patch category (or check all of '
                    'them to apply everything).'
                )
            cfg.patch_categories = selected
        cfg.exclude_categories = set()
        return cfg

    def _apply_config(self, cfg: PatcherConfig) -> None:
        self.backup_cb.setChecked(cfg.backup)
        self.backup_dir_edit.setText(
            str(cfg.backup_dir) if cfg.backup_dir else ''
        )
        self.verify_cb.setChecked(cfg.verify_patch)
        self.allow_incomplete_cb.setChecked(cfg.allow_incomplete)
        self.dry_run_cb.setChecked(cfg.dry_run)
        self.log_level_combo.setCurrentText(cfg.log_level.name)

        if cfg.patch_categories:
            for name, cb in self.category_boxes.items():
                cb.setChecked(name in cfg.patch_categories)
        else:
            for cb in self.category_boxes.values():
                cb.setChecked(True)

    # ------------------------------------------------------------------
    # Patch flow
    # ------------------------------------------------------------------
    def _on_cert_mode_changed(self) -> None:
        mode = self.cert_combo.currentData()
        if mode is None:
            self.run_btn.setText('Patch image')
            self.cert_hint_lbl.setText('')
        else:
            self.run_btn.setText('Patch + re-sign image')
            self.cert_hint_lbl.setText(
                'Applies the patches (needles and, if enabled, security '
                'policies), then re-signs every modified partition with '
                'the certificate bypass.'
            )

    def _output_path(self) -> Path | None:
        text = self.output_edit.text().strip()
        return Path(text) if text else None

    def _create_backup(self, image_path: Path, backup_dir: Path | None) -> Path:
        """Create a timestamped backup (same behaviour as the CLI)."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_name = (
            f'{image_path.stem}_backup_{timestamp}{image_path.suffix}'
        )
        if backup_dir:
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / backup_name
        else:
            backup_path = image_path.parent / backup_name
        shutil.copy2(image_path, backup_path)
        return backup_path

    def _run_patch(self) -> None:
        if self._busy or self.patcher is None:
            return
        output = self._output_path()
        if output is None:
            self._warn('Please choose an output file first (top of the '
                       'window).')
            return

        try:
            cfg = self._sync_config(strict=True)
        except ValueError as exc:
            self._warn(str(exc))
            return

        patcher = self.patcher
        image_path = self.image_path
        cert_mode = self.cert_combo.currentData()
        patch_policies = self.patch_policies_cb.isChecked()
        dry = cfg.dry_run

        def job() -> dict[str, Any]:
            result: dict[str, Any] = {
                'dry': dry,
                'output': str(output),
                'backup': None,
            }
            if cfg.backup and not dry:
                result['backup'] = str(
                    self._create_backup(image_path, cfg.backup_dir)
                )
            if cert_mode is not None:
                # Patch first (needles + optional policies), then re-sign
                # every partition whose contents changed. Skipping the
                # patch step would find "nothing to re-sign" on a freshly
                # loaded image.
                patched = patcher.patch(
                    output, patch_policies=patch_policies
                )
                report_path = patched.with_suffix('.patch_report.json')
                if report_path.is_file():
                    result['report'] = json.loads(report_path.read_text())

                if dry:
                    self.log(f'DRY RUN: would re-sign ({cert_mode}) and '
                             f'save to {output}')
                    result['signed'] = []
                    return result

                mode = CertBypassMode(cert_mode)
                signed = patcher.apply_cert_bypass(mode)
                result['signed'] = signed
                if signed:
                    patcher.image.save(output)
                return result

            patcher.patch(output, patch_policies=patch_policies)
            report_path = Path(output).with_suffix('.patch_report.json')
            if report_path.is_file():
                result['report'] = json.loads(report_path.read_text())
            return result

        self._worker(job, on_done=self._on_patch_done)

    def _on_patch_done(self, result: dict[str, Any]) -> None:
        if result.get('backup'):
            self.log(f'Created backup at {result["backup"]}')

        if 'signed' in result:
            signed = result['signed']
            if result['dry']:
                self._info(
                    'Dry run complete',
                    'Dry run finished: no files were written.',
                )
            elif signed:
                report = result.get('report', {})
                applied = report.get('applied_patches', '?')
                total = report.get('total_patches', '?')
                self._info(
                    'Patch + re-sign complete',
                    f'Applied {applied}/{total} patches.\n'
                    'Re-signed partition(s): '
                    + ', '.join(signed)
                    + f'\n\nOutput: {result["output"]}',
                )
            else:
                self._info(
                    'Nothing to re-sign',
                    'No partition changed after patching, so no certificate '
                    'needed re-signing.\n\nThe image may use different code '
                    'patterns; check the log (DEBUG level) and consider '
                    'custom patches.',
                )
        else:
            report = result.get('report', {})
            applied = report.get('applied_patches', '?')
            total = report.get('total_patches', '?')
            skipped = report.get('skipped_patches', '?')
            if result['dry']:
                self._info(
                    'Dry run complete',
                    f'Dry run: would apply {applied}/{total} patches '
                    f'({skipped} skipped).\nNo files were written.',
                )
            else:
                self._info(
                    'Patching complete',
                    f'Applied {applied}/{total} patches '
                    f'({skipped} skipped).\n'
                    f'Output: {result["output"]}',
                )
            if not report.get('applied_patches'):
                self.log(
                    '[WARNING] No patches were applied - the image may use '
                    'different code patterns. Check the log (DEBUG level) '
                    'and the upstream guide for custom patches.'
                )

        # Reload the ORIGINAL image so options can be tweaked and the
        # patch run repeated.
        if not result['dry'] and self.image_path is not None:
            self.load_image_path(self.image_path)

    # ------------------------------------------------------------------
    # Partition table / info / dump / extract
    # ------------------------------------------------------------------
    def refresh_partitions(self) -> None:
        """Refresh the partition table and the partition combo boxes."""
        table = self.part_table
        table.setRowCount(0)
        self.replace_combo.clear()
        self.remove_combo.clear()
        self.cert_part_combo.clear()

        if self.patcher is None:
            return

        for row, (name, partition) in enumerate(
            self.patcher.image.partitions.items()
        ):
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(name))
            table.setItem(
                row, 1,
                QTableWidgetItem(
                    f'{len(partition.data)} bytes '
                    f'({human_size(len(partition.data))})'
                ),
            )
            table.setItem(
                row, 2,
                QTableWidgetItem(
                    f'0x{partition.header.memory_address:08x}'
                ),
            )
            table.setItem(
                row, 3,
                QTableWidgetItem(
                    'yes' if partition.header.is_extended else 'no'
                ),
            )
            table.setItem(
                row, 4,
                QTableWidgetItem(
                    ', '.join(c.header.name for c in partition.certs) or '-'
                ),
            )
            self.replace_combo.addItem(name)
            self.remove_combo.addItem(name)
            self.cert_part_combo.addItem(name)

        self._update_enablements()

    def _selected_partition_name(self) -> str | None:
        row = self.part_table.currentRow()
        if row < 0:
            return None
        item = self.part_table.item(row, 0)
        return item.text() if item else None

    def _partition_info(self) -> None:
        if self.patcher is None:
            return
        name = self._selected_partition_name()
        if name is None:
            self._warn('Select a partition in the table first.')
            return
        partition = self.patcher.image.partitions.get(name)
        if partition is None:
            return

        preview_size = min(64, len(partition.data))
        hex_bytes = ' '.join(f'{b:02x}' for b in partition.data[:preview_size])
        lines = [
            str(partition),
            '',
            f'Data size: {len(partition.data)} bytes',
            f'Certs: {len(partition.certs)}',
            f'Data preview: {hex_bytes}'
            + ('...' if len(partition.data) > preview_size else ''),
        ]
        self.details_view.setPlainText('\n'.join(lines))
        self._status(f'Partition info: {name}')

    def _dump_partition_dialog(self) -> None:
        name = self._selected_partition_name()
        if name is None or self.patcher is None:
            return
        stem = self.image_path.stem if self.image_path else 'lk'
        suggested = f'{stem}_{name}.bin'
        path, _ = QFileDialog.getSaveFileName(
            self, 'Dump partition to file', suggested, ''
        )
        if not path:
            return
        self._dump_partition_to(name, Path(path))

    def _dump_partition_to(self, name: str, path: Path) -> None:
        if self.patcher is None:
            return
        partition = self.patcher.image.partitions.get(name)

        if partition is None:

            def missing() -> None:
                self._warn(f'Partition not found: {name}')

            missing()
            return

        def job() -> str:
            partition.save(path)
            return str(partition)

        def on_done(_details: str) -> None:
            self.log(f'Dumped partition "{name}" to {path}')
            self.details_view.setPlainText(str(partition))
            self._status(f'Dumped {name}')

        self._worker(job, on_done=on_done)

    def _extract_all_dialog(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, 'Select directory for extracted partitions'
        )
        if not directory:
            return
        self._extract_all_to(Path(directory))

    def _extract_all_to(self, directory: Path) -> None:
        if self.patcher is None:
            return

        def job() -> list[Path]:
            return self.patcher.extract_all_partitions(directory)

        def on_done(paths: list[Path]) -> None:
            self.log(f'Extracted {len(paths)} partitions to {directory}')
            self._info(
                'Extraction complete',
                f'Extracted {len(paths)} partitions to:\n{directory}',
            )

        self._worker(job, on_done=on_done)

    # ------------------------------------------------------------------
    # Partition modification
    # ------------------------------------------------------------------
    def _parse_address(self, text: str, *, required: bool = False) -> int | None:
        text = text.strip()
        if not text:
            if required:
                raise ValueError('A memory address is required.')
            return None
        try:
            value = int(text, 0)
        except ValueError as exc:
            raise ValueError(
                f'Invalid address "{text}" - use hex (0x...) or decimal.'
            ) from exc
        if value < 0 or value > 0xFFFFFFFFFFFFFFFF:
            raise ValueError(f'Address out of range: {text}')
        return value

    def _set_modified(self, modified: bool = True) -> None:
        self._modified = modified
        if self.image_path is not None:
            self.status_image_lbl.setText(
                f'{self.image_path.name} (modified - not saved)'
                if modified
                else f'{self.image_path.name} (not modified)'
            )

    def _add_partition(self) -> None:
        if self._busy or self.patcher is None:
            return
        name = self.add_name_edit.text().strip()
        file_text = self.add_file_edit.text().strip()
        if not name:
            self._warn('Enter a name for the new partition.')
            return
        if len(name) > 32:
            self._warn('Partition name is limited to 32 characters.')
            return
        if name in self.patcher.image.partitions:
            self._warn(f'Partition "{name}" already exists in the image.')
            return
        data_file = Path(file_text) if file_text else None
        if data_file is None or not data_file.is_file():
            self._warn('Choose a valid data file for the new partition.')
            return
        try:
            address = self._parse_address(
                self.add_addr_edit.text(), required=False
            )
        except ValueError as exc:
            self._warn(str(exc))
            return
        use_extended = not self.add_legacy_cb.isChecked()

        def job() -> int:
            data = data_file.read_bytes()
            self.patcher.image.add_partition(
                name=name,
                data=data,
                memory_address=address if address is not None else 0,
                use_extended=use_extended,
            )
            return len(data)

        def on_done(size: int) -> None:
            self.log(f'Added partition "{name}" ({size} bytes)')
            self._set_modified()
            self.refresh_partitions()

        self._worker(job, on_done=on_done)

    def _replace_partition(self) -> None:
        if self._busy or self.patcher is None:
            return
        name = self.replace_combo.currentText()
        if not name:
            self._warn('Select the partition to replace.')
            return
        file_text = self.replace_file_edit.text().strip()
        data_file = Path(file_text) if file_text else None
        if data_file is None or not data_file.is_file():
            self._warn('Choose a valid data file with the new contents.')
            return
        try:
            address = self._parse_address(
                self.replace_addr_edit.text(), required=False
            )
        except ValueError as exc:
            self._warn(str(exc))
            return

        def job() -> int:
            data = data_file.read_bytes()
            self.patcher.replace_partition(
                name=name, data=data, memory_address=address
            )
            return len(data)

        def on_done(size: int) -> None:
            self.log(f'Replaced partition "{name}" ({size} bytes)')
            self._set_modified()
            self.refresh_partitions()

        self._worker(job, on_done=on_done)

    def _remove_partition(self) -> None:
        if self._busy or self.patcher is None:
            return
        name = self.remove_combo.currentText()
        if not name:
            self._warn('Select the partition to remove.')
            return
        if not self._confirm(
            f'Remove partition "{name}" from the image?\n'
            'The change is kept in memory until you save the image.'
        ):
            return

        def job() -> None:
            self.patcher.image.remove_partition(name)

        def on_done(_none: object) -> None:
            self.log(f'Removed partition "{name}"')
            self._set_modified()
            self.refresh_partitions()

        self._worker(job, on_done=on_done)

    def _add_certificate(self) -> None:
        if self._busy or self.patcher is None:
            return
        part_name = self.cert_part_combo.currentText()
        if not part_name:
            self._warn('Select the target partition.')
            return
        file_text = self.cert_file_edit.text().strip()
        cert_file = Path(file_text) if file_text else None
        if cert_file is None or not cert_file.is_file():
            self._warn('Choose a valid certificate file.')
            return
        cert_type = self.cert_type_combo.currentText()

        def job() -> int:
            cert_data = cert_file.read_bytes()
            partition = self.patcher.image.partitions[part_name]
            partition.add_certificate(cert_data, cert_type)
            self.patcher.image._rebuild_contents()  # noqa: SLF001 - same as CLI
            return len(cert_data)

        def on_done(size: int) -> None:
            self.log(
                f'Added {cert_type} ({size} bytes) to partition "{part_name}"'
            )
            self._set_modified()
            self.refresh_partitions()

        self._worker(job, on_done=on_done)

    def _save_modified(self) -> None:
        if self._busy or self.patcher is None:
            return
        output = self._output_path()
        if output is None:
            self._warn('Please choose an output file first (top of the '
                       'window).')
            return
        dry = self.dry_run_cb.isChecked()
        patcher = self.patcher

        if dry:
            self.log(f'DRY RUN: would save modified image to {output}')
            self._info(
                'Dry run',
                f'Dry run: the modified image would be saved to {output}.',
            )
            return

        def job() -> str:
            patcher.image.save(output)
            return str(output)

        def on_done(path: str) -> None:
            self.log(f'Modified image saved to {path}')
            self._set_modified(False)
            self._status(f'Saved {Path(path).name}')
            self._info(
                'Image saved',
                f'Modified image saved to:\n{path}',
            )

        self._worker(job, on_done=on_done)

    # ------------------------------------------------------------------
    # Security policies
    # ------------------------------------------------------------------
    def _analyze_policies(self) -> None:
        if self._busy or self.patcher is None:
            return
        patcher = self.patcher

        def job() -> dict[str, Any]:
            lk_partition = patcher.image.partitions.get('lk')
            if lk_partition is None:
                return {'error': 'No LK partition found in this image.'}
            return analyze_lk_security_policies(lk_partition)

        self._worker(job, on_done=self._on_policies_analyzed)

    def _on_policies_analyzed(self, result: dict[str, Any]) -> None:
        table = self.policy_table
        table.setRowCount(0)

        if 'error' in result:
            self.policy_info_lbl.setText(result['error'])
            self._warn(result['error'])
            return

        info_bits = [
            f'Load address: {result.get("load_address", "?")}',
            f'Policy table: '
            f'{result.get("policy_table_offset", "not found")}',
        ]
        if 'warning' in result:
            info_bits.append(f'Warning: {result["warning"]}')

        if result.get('policy_table_found'):
            for row, policy in enumerate(result['policies']):
                table.insertRow(row)
                table.setItem(row, 0, QTableWidgetItem(policy['name']))
                for col, key in enumerate(
                    ['nosbc_lock', 'nosbc_unlock', 'sbc_lock', 'sbc_unlock'],
                    start=1,
                ):
                    item = QTableWidgetItem(str(policy[key]))
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignCenter
                    )
                    table.setItem(row, col, item)
        else:
            info_bits.append(
                'No security policy table found in the LK partition.'
            )

        self.policy_info_lbl.setText('  |  '.join(info_bits))
        self.log(f'Security policy analysis: {info_bits[1]}')

    def _goto_patch_policies(self) -> None:
        self.tabs.setCurrentIndex(0)
        self.patch_policies_cb.setChecked(True)
        self._status('"Patch security policies" enabled on the Patch tab')

    # ------------------------------------------------------------------
    # Config dialogs
    # ------------------------------------------------------------------
    def _load_config_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Load configuration', '', JSON_FILTER
        )
        if not path:
            return
        self._load_config_from(Path(path))

    def _load_config_from(self, path: Path) -> None:
        try:
            cfg = PatcherConfig.from_file(path)
        except LkPatcherError as exc:
            self._error(f'Failed to load configuration:\n{exc}')
            return
        self._apply_config(cfg)
        self.log(f'Loaded configuration from {path}')

    def _save_config_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save configuration', 'lkpatcher-config.json', JSON_FILTER
        )
        if not path:
            return
        self._save_config_to(Path(path))

    def _save_config_to(self, path: Path) -> None:
        try:
            cfg = self._sync_config(strict=False)
            data = cfg.to_dict()
            if self.patcher is not None:
                data['available_patches'] = {
                    category: list(needles)
                    for category, needles in
                    self.patcher.patch_manager.patches.items()
                }
            path.write_text(json.dumps(data, indent=4))
            self.log(f'Saved configuration to {path}')
            self._status(f'Configuration saved: {path.name}')
        except (LkPatcherError, OSError, ValueError) as exc:
            self._error(f'Failed to save configuration:\n{exc}')

    def _export_config_dialog(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, 'Export default configuration',
            'lkpatcher-defaults.json', JSON_FILTER,
        )
        if not path:
            return
        self._export_config_to(Path(path))

    def _export_config_to(self, path: Path) -> None:
        try:
            data = PatcherConfig().to_dict()
            patches = (
                self.patcher.patch_manager.patches
                if self.patcher is not None
                else PatchManager().patches
            )
            data['available_patches'] = {
                category: list(needles)
                for category, needles in patches.items()
            }
            path.write_text(json.dumps(data, indent=4))
            self.log(f'Exported default configuration to {path}')
            self._status(f'Default configuration exported: {path.name}')
        except (LkPatcherError, OSError) as exc:
            self._error(f'Failed to export configuration:\n{exc}')

    # ------------------------------------------------------------------
    # Log tab
    # ------------------------------------------------------------------
    def _on_log_level_changed(self, level: str) -> None:
        self._log_handler.setLevel(LogLevel.from_string(level)
                                   .to_logging_level())

    def _save_log(self) -> None:
        default = f'lkpatcher-gui-{datetime.now():%Y%m%d_%H%M%S}.log'
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save log', default, LOG_FILTER
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_view.toPlainText())
            self._status(f'Log saved: {Path(path).name}')
        except OSError as exc:
            self._error(f'Failed to save log:\n{exc}')

    def _clear_log(self) -> None:
        self.log_view.clear()

    # ------------------------------------------------------------------
    # About
    # ------------------------------------------------------------------
    def _about(self) -> None:
        QMessageBox.about(
            self,
            'About lkpatcher GUI',
            f'<b>lkpatcher GUI</b> v{APP_VERSION}<br>'
            'A cross-platform (Windows / Linux) PySide6 frontend for '
            'lkpatcher.<br><br>'
            '<b>GUI</b> by '
            '<a href="https://github.com/ILYESwsa">ILYESwsa</a><br><br>'
            f'<b>lkpatcher</b> v{LKPATCHER_VERSION} by Roger Ortiz '
            '(R0rt1z2)<br>'
            '<a href="https://github.com/R0rt1z2/lkpatcher">'
            'github.com/R0rt1z2/lkpatcher</a><br><br>'
            'Both projects are licensed under the GPL-3.0-or-later.<br><br>'
            '<small>Use at your own risk - always keep backups of your '
            'bootloader images.</small>',
        )


def main() -> int:
    """Application entry point."""
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG_NAME)
    app.setApplicationVersion(APP_VERSION)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == '__main__':
    sys.exit(main())
