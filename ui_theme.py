"""Shared navy / lavender / mint presentation for the Tk desktop interface."""
import tkinter as tk
from tkinter import ttk


COLORS = {
    'canvas': '#F5F6FA', 'surface': '#FFFFFF', 'ink': '#18283F',
    'muted': '#667085', 'line': '#E4E6EF', 'navy': '#0C2034',
    'navy_soft': '#142D44', 'on_navy': '#F5F7FC', 'header_muted': '#B6C5D8',
    'accent': '#7756C4', 'accent_hover': '#6544B0', 'accent_pressed': '#563999',
    'lavender': '#F0EBFA', 'lavender_line': '#D4C7EF',
    'mint': '#69D3B0', 'success': '#247A62', 'mint_soft': '#EAF7F1',
    'warning': '#896225', 'warning_soft': '#FBF5E9', 'disabled': '#858C9B',
}
FONT = 'Segoe UI'
DISPLAY_FONT = 'Segoe UI Variable Display'


def apply_theme(root):
    """Use clam so Windows' native theme cannot override the shared palette."""
    c = COLORS
    style = ttk.Style(root)
    style.theme_use('clam')
    root.configure(background=c['canvas'])
    root.option_add('*Font', (FONT, 10))
    root.option_add('*TCombobox*Listbox.background', c['surface'])
    root.option_add('*TCombobox*Listbox.foreground', c['ink'])
    root.option_add('*TCombobox*Listbox.selectBackground', c['lavender'])
    root.option_add('*TCombobox*Listbox.selectForeground', c['accent_pressed'])
    root.option_add('*TCombobox*Listbox.font', (FONT, 10))
    style.configure('.', font=(FONT, 10), background=c['surface'], foreground=c['ink'],
                    bordercolor=c['line'], lightcolor=c['surface'], darkcolor=c['line'],
                    focuscolor=c['accent'])
    style.configure('TFrame', background=c['surface'])
    style.configure('TLabel', background=c['surface'], foreground=c['ink'])
    style.configure('Muted.TLabel', foreground=c['muted'])
    style.configure('TLabelframe', background=c['surface'], bordercolor=c['line'],
                    lightcolor=c['line'], darkcolor=c['line'], borderwidth=1, relief='solid')
    style.configure('TLabelframe.Label', font=(FONT, 10, 'bold'), foreground=c['ink'],
                    background=c['surface'])
    style.configure('TButton', padding=(12, 7), background=c['surface'],
                    foreground=c['ink'], borderwidth=1, relief='flat',
                    bordercolor=c['line'], lightcolor=c['surface'], darkcolor=c['surface'],
                    focusthickness=2, focuscolor=c['accent'])
    style.map('TButton', background=[('disabled', c['canvas']), ('pressed', c['lavender_line']),
                                   ('active', c['lavender'])],
              foreground=[('disabled', c['disabled'])],
              bordercolor=[('focus', c['accent']), ('active', c['lavender_line'])])
    style.configure('Primary.TButton', background=c['accent'], foreground=c['surface'],
                    bordercolor=c['accent'], lightcolor=c['accent'], darkcolor=c['accent'],
                    font=(FONT, 10, 'bold'), focuscolor=c['surface'])
    style.map('Primary.TButton', background=[('disabled', c['line']),
              ('pressed', c['accent_pressed']), ('active', c['accent_hover'])],
              foreground=[('disabled', c['disabled']), ('!disabled', c['surface'])],
              bordercolor=[('focus', c['navy']), ('!disabled', c['accent'])],
              lightcolor=[('!disabled', c['accent'])], darkcolor=[('!disabled', c['accent'])])
    style.configure('TEntry', padding=6, fieldbackground=c['surface'],
                    foreground=c['ink'], insertcolor=c['ink'], bordercolor=c['line'])
    style.map('TEntry', bordercolor=[('focus', c['accent'])],
              fieldbackground=[('disabled', c['canvas'])], foreground=[('disabled', c['disabled'])])
    style.configure('TCombobox', padding=6, arrowsize=13, fieldbackground=c['surface'],
                    background=c['canvas'], foreground=c['ink'], arrowcolor=c['accent'])
    style.map('TCombobox', fieldbackground=[('readonly', c['surface'])],
              foreground=[('disabled', c['disabled']), ('readonly', c['ink'])],
              selectbackground=[('readonly', c['lavender'])],
              selectforeground=[('readonly', c['ink'])],
              bordercolor=[('focus', c['accent']), ('active', c['lavender_line'])])
    for name in ('TCheckbutton', 'TRadiobutton'):
        style.configure(name, padding=(0, 4), background=c['surface'],
                        indicatorbackground=c['surface'], indicatorforeground=c['surface'],
                        indicatorcolor=c['surface'], focuscolor=c['accent'])
        style.map(name, background=[('active', c['surface'])],
                  foreground=[('disabled', c['disabled']), ('active', c['accent_pressed'])],
                  indicatorbackground=[('selected', c['accent'])],
                  indicatorcolor=[('selected', c['accent'])])
    style.configure('TNotebook', background=c['surface'], borderwidth=0, tabmargins=(0, 0, 0, 8))
    style.configure('TNotebook.Tab', padding=(12, 9), background=c['surface'],
                    foreground=c['muted'], borderwidth=0, focuscolor=c['accent'])
    style.map('TNotebook.Tab', background=[('selected', c['lavender']), ('active', c['canvas'])],
              foreground=[('selected', c['accent_pressed']), ('active', c['ink'])])
    for direction in ('Vertical', 'Horizontal'):
        style.configure(direction + '.TScrollbar', background=c['lavender_line'],
                        troughcolor=c['canvas'], borderwidth=0, arrowsize=12,
                        arrowcolor=c['muted'], lightcolor=c['lavender_line'], darkcolor=c['lavender_line'])
        style.map(direction + '.TScrollbar', background=[('active', c['accent'])])
    style.configure('Header.TFrame', background=c['navy'])
    style.configure('Header.TLabel', background=c['navy'], foreground=c['on_navy'])
    style.configure('HeaderTitle.TLabel', background=c['navy'], foreground=c['on_navy'],
                    font=(DISPLAY_FONT, 23, 'bold'))
    style.configure('HeaderSub.TLabel', background=c['navy'], foreground=c['header_muted'], font=(FONT, 10))
    style.configure('HeaderTag.TLabel', background=c['navy_soft'], foreground=c['mint'], padding=(10, 6),
                    font=(FONT, 9, 'bold'))
    style.configure('Status.TFrame', background=c['mint_soft'])
    style.configure('Status.TLabel', background=c['mint_soft'], foreground=c['success'], font=(FONT, 10, 'bold'))
    style.configure('StatusDetail.TLabel', background=c['mint_soft'], foreground=c['muted'], font=(FONT, 9))
    style.configure('Metric.TLabel', foreground=c['accent'], font=(DISPLAY_FONT, 28, 'bold'))
    style.configure('MetricMint.TLabel', foreground=c['success'], font=(DISPLAY_FONT, 28, 'bold'))
    return style


def style_text(widget):
    widget.configure(background=COLORS['canvas'], foreground=COLORS['ink'],
                     insertbackground=COLORS['ink'], selectbackground=COLORS['lavender_line'],
                     selectforeground=COLORS['ink'], relief='flat', borderwidth=0,
                     highlightthickness=1, highlightbackground=COLORS['line'],
                     highlightcolor=COLORS['accent'], padx=12, pady=10, spacing1=2, spacing3=2)


def responsive_labels(parent):
    """Keep existing explanatory copy within the available panel width."""
    for child in parent.winfo_children():
        if isinstance(child, ttk.Label) and (int(child.cget('wraplength') or 0) > 0
                                            or len(str(child.cget('text'))) > 80):
            owner = child.master
            owner.bind('<Configure>', lambda event, label=child:
                       label.configure(wraplength=max(180, event.width - 36)), add='+')
        responsive_labels(child)

