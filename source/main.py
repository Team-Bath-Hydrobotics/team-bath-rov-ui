import os
import sys
import faulthandler
import io
import traceback
from cProfile import Profile
from pstats import SortKey, Stats
from contextlib import redirect_stdout, redirect_stderr

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QSurfaceFormat

script_dir = os.path.dirname(os.path.abspath(__file__))  # Get the script's directory
os.chdir(script_dir)  # Change working directory to the script's location

from app import App
#
# os.environ["QT_QUICK_BACKEND"] = "software"  # Force CPU rendering (Qt Quick)
# os.environ["T_QPA_PLATFORM"] = "offscreen"  # Alternative for headless rendering (if needed)
# App.setAttribute(Qt.ApplicationAttribute.AA_UseSoftwareOpenGL)
#App.setAttribute(Qt.ApplicationAttribute.AA_UseOpenGLES)

#App.setAttribute(Qt.ApplicationAttribute.AA_UseDesktopOpenGL, True)

# fmt = QSurfaceFormat()
# fmt.setRenderableType(QSurfaceFormat.RenderableType.OpenGL)  # Prefer OpenGL over OpenGL ES
# fmt.setSwapBehavior(QSurfaceFormat.SwapBehavior.SingleBuffer)  # Common in software renderers
# QSurfaceFormat.setDefaultFormat(fmt)

faulthandler.enable()

DEBUG = False  # STDOUT will not be displayed in the UI when set to True.

try:
    with Profile() as profile:
        # Catch standard output
        if DEBUG:
            app = App(sys.__stdout__, sys.__stderr__, sys.argv)
            exit_code = app.exec()
        else:
            stderr_io = io.StringIO()
            with redirect_stderr(stderr_io) as redirected_stderr:
                stdout_io = io.StringIO()
                with redirect_stdout(stdout_io) as redirected_stdout:
                    app = App(redirected_stdout, redirected_stderr, sys.argv)
                    exit_code = app.exec()
                    print(exit_code, file=sys.__stderr__)

        Stats(profile).strip_dirs().sort_stats(SortKey.TIME).print_stats()
        sys.exit(exit_code)
except Exception as e:
    traceback.print_exception(e, file=sys.__stderr__)
