import os
import sys
import json
import re
import traceback
import logging
import webbrowser
import copy
import pickle
import platform

from PyQt4.QtGui import (QMainWindow, QMessageBox,
                         QApplication, QFileDialog, QInputDialog,
                         QLineEdit, QMenu, QDrag, QPainter, QPen,
                         QPalette, QDesktopServices, QFont, QAction,
                         QPixmap, QFileSystemModel, QHeaderView)
from PyQt4.QtCore import (Qt, pyqtSignature, SIGNAL, QMimeData,
                          QSettings, QCoreApplication, QUrl)

from spyderlib.widgets.internalshell import InternalShell
from spyderlib.widgets.externalshell.namespacebrowser import NamespaceBrowser
from spyderlib.widgets.sourcecode.codeeditor import CodeEditor

import spykeutils
from spykeutils.plugin.data_provider import DataProvider
from spykeutils.plugin.analysis_plugin import AnalysisPlugin
from spykeutils.progress_indicator import CancelException
from spykeutils import SpykeException

from main_ui import Ui_MainWindow
from settings import SettingsWindow
from filter_dock import FilterDock
from filter_dialog import FilterDialog
from filter_group_dialog import FilterGroupDialog
from progress_indicator_dialog import ProgressIndicatorDialog
from plugin_editor_dock import PluginEditorDock
import ipython_connection as ipy
from plugin_model import PluginModel


logger = logging.getLogger('spykeviewer')
ch = logging.StreamHandler()
ch.setLevel(logging.WARNING)
logger.addHandler(ch)


#noinspection PyCallByClass,PyTypeChecker,PyArgumentList
class MainWindow(QMainWindow, Ui_MainWindow):
    """ The main window of Spyke Viewer.
    """

    def __init__(self, parent=None):
        QMainWindow.__init__(self, parent)

        QCoreApplication.setOrganizationName('SpykeUtils')
        QCoreApplication.setApplicationName('Spyke Viewer')
        self.data_path = QDesktopServices.storageLocation(
            QDesktopServices.DataLocation)
        self.startup_script = os.path.join(self.data_path, 'startup.py')

        self.setupUi(self)
        self.dir = os.getcwd()

        # Configuration
        self.config = {}
        self.config['ask_plugin_path'] = True

        # Python console
        self.console = None
        self.progress = ProgressIndicatorDialog(self)
        self.provider_factory = DataProvider
        self.selections = []
        self.provider = None
        self.plugin_paths = []
        self.init_python()

        # IPython menu option
        self.ipy_kernel = None
        if ipy.ipython_available:
            a = QAction('New IPython console', self.menuFile)
            self.menuFile.insertAction(self.actionSettings, a)
            self.connect(a, SIGNAL('triggered()'),
                         self.on_actionIPython_triggered)

        # Drag and Drop for selections menu
        self.menuSelections.setAcceptDrops(True)
        self.menuSelections.paintEvent =\
            self.on_menuSelections_paint
        self.menuSelections.mousePressEvent =\
            self.on_menuSelections_mousePressed
        self.menuSelections.mouseMoveEvent =\
            self.on_menuSelections_mouseMoved
        self.menuSelections.dragEnterEvent =\
            self.on_menuSelections_dragEnter
        self.menuSelections.dragMoveEvent =\
            self.on_menuSelections_dragMoved
        self.menuSelections.dropEvent =\
            self.on_menuSelections_drop

        self.seldrag_start_pos = None
        self.seldrag_selection = None
        self.seldrag_target = None
        self.seldrag_target_upper = False

        # Hide "Clear cache" entry - not useful for now because of
        # Neo memory leak
        self.actionClearCache.setVisible(False)

        # Filters
        settings = QSettings()
        if not settings.contains('filterPath'):
            data_path = QDesktopServices.storageLocation(
                QDesktopServices.DataLocation)
            self.filter_path = os.path.join(data_path, 'filters')
        else:
            self.filter_path = settings.value('filterPath')

        filter_types = self.get_filter_types()

        self.filterDock = FilterDock(self.filter_path, filter_types,
                                     menu=self.menuFilter, parent=self)
        self.filterDock.setObjectName('filterDock')
        self.filterDock.current_filter_changed.connect(
            self.on_current_filter_changed)
        self.filterDock.filters_changed.connect(
            self.on_filters_changed)
        self.addDockWidget(Qt.RightDockWidgetArea, self.filterDock)

        self.show_filter_exceptions = True

        # Plugin Editor
        self.pluginEditorDock = PluginEditorDock()
        self.pluginEditorDock.setObjectName('editorDock')
        self.addDockWidget(Qt.RightDockWidgetArea, self.pluginEditorDock)
        self.pluginEditorDock.setVisible(False)
        self.pluginEditorDock.plugin_saved.connect(self.plugin_saved)
        self.pluginEditorDock.file_available.connect(self.on_file_available)

        self.consoleDock.edit_script = lambda (path): \
            self.pluginEditorDock.add_file(path)

        from spyderlib.utils.misc import get_error_match

        def p(x):
            match = get_error_match(unicode(x))
            if match:
                fname, lnb = match.groups()
                self.pluginEditorDock.show_position(fname, int(lnb))

        self.connect(self.console, SIGNAL("go_to_error(QString)"), p)

        # File navigation
        self.file_system_model = QFileSystemModel()
        self.file_system_model.setRootPath('')
        self.fileTreeView.setModel(self.file_system_model)
        self.fileTreeView.setCurrentIndex(
            self.file_system_model.index(self.dir))
        self.fileTreeView.expand(self.file_system_model.index(self.dir))

        self.fileTreeView.setColumnHidden(1, True)
        self.fileTreeView.setColumnHidden(2, True)
        self.fileTreeView.setColumnHidden(3, True)

        self.fileTreeView.header().setResizeMode(QHeaderView.ResizeToContents)

        # Docks
        self.setCentralWidget(None)
        self.update_view_menu()

        # Finish initialization if we are not a subclass
        if type(self) is MainWindow:
            self.finish_initialization()

    ##### Startup ########################################################
    def finish_initialization(self):
        """ This should to be called at the end of the initialization phase
        of the program (e.g. at the end of the ``__init__()`` method of a
        domain-specific subclass).
        """
        self.update_view_menu()
        self.restore_state()
        self.run_startup_script()
        self.reload_plugins()
        self.load_plugin_configs()
        self.load_current_selection()

    def get_filter_types(self):
        """ Return a list of filter type tuples as required by
            :class:`filter_dock.FilterDock. Override in domain-specific
            subclass.
        """
        return []

    def update_view_menu(self):
        """ Recreate the "View" menu.
        """
        if hasattr(self, 'menuView'):
            a = self.menuView.menuAction()
            self.mainMenu.removeAction(a)
        self.menuView = self.createPopupMenu()
        self.menuView.setTitle('View')
        self.mainMenu.insertMenu(self.menuHelp.menuAction(), self.menuView)

    def set_default_plugin_path(self):
        """ Set the default plugin path (contains the standard plugins
        after installation).
        """
        if hasattr(sys, 'frozen'):
            module_path = os.path.dirname(sys.executable)
        else:
            file_path = os.path.abspath(os.path.dirname(__file__))
            module_path = os.path.dirname(file_path)
        plugin_path = os.path.join(module_path, 'plugins')

        if os.path.isdir(plugin_path):
            self.plugin_paths.append(plugin_path)
        else:
            logger.warning('Plugin path "%s" does not exist, no plugin '
                           'path set!' %
                           plugin_path)

    def restore_state(self):
        """ Restore previous state of the GUI and settings from saved
        configuration.
        """
        settings = QSettings()
        if not settings.contains('windowGeometry') or \
                not settings.contains('windowState'):
            self.set_initial_layout()
        else:
            self.restoreGeometry(settings.value('windowGeometry'))
            self.restoreState(settings.value('windowState'))

        if not settings.contains('pluginPaths'):
            self.set_default_plugin_path()
        else:
            paths = settings.value('pluginPaths')
            self.plugin_paths = []
            if paths is not None:
                for p in paths:
                    if not os.path.isdir(p):
                        logger.warning('Plugin path "%s" does not exist, '
                                       'removing from configuration...' % p)
                    else:
                        self.plugin_paths.append(p)
            
            if not self.plugin_paths:
                logger.warning('No plugin paths set! Setting default path...')
                self.set_default_plugin_path()

        if not settings.contains('selectionPath'):
            self.selection_path = os.path.join(self.data_path, 'selections')
        else:
            self.selection_path = settings.value('selectionPath')

        if not settings.contains('dataPath'):
            AnalysisPlugin.data_dir = os.path.join(self.data_path, 'data')
        else:
            AnalysisPlugin.data_dir = settings.value('dataPath')

        if not settings.contains('remoteScript') or not os.path.isfile(
                settings.value('remoteScript')):
            if settings.contains('remoteScript'):
                logger.warning('Remote script not found! Reverting to '
                               'default location...')
            if hasattr(sys, 'frozen'):
                path = os.path.dirname(sys.executable)
            else:
                path = os.path.dirname(spykeutils.__file__)
                path = os.path.join(os.path.abspath(path), 'plugin')
            self.remote_script = os.path.join(path, 'startplugin.py')
        else:
            self.remote_script = settings.value('remoteScript')

        if self.plugin_paths:
            self.pluginEditorDock.set_default_path(self.plugin_paths[-1])

    def set_initial_layout(self):
        """ Set an initial layout for the docks (when no previous
        configuration could be loaded).
        """
        self.filesDock.setMinimumSize(100, 100)
        self.resize(800, 750)

        self.removeDockWidget(self.filesDock)
        self.removeDockWidget(self.filterDock)
        self.removeDockWidget(self.pluginDock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.filesDock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.filterDock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.pluginDock)
        self.tabifyDockWidget(self.filterDock, self.pluginDock)
        self.filesDock.setVisible(True)
        self.filterDock.setVisible(True)
        self.pluginDock.setVisible(True)

        self.consoleDock.setVisible(False)
        self.variableExplorerDock.setVisible(False)
        self.historyDock.setVisible(False)
        self.tabifyDockWidget(self.consoleDock, self.variableExplorerDock)
        self.tabifyDockWidget(self.variableExplorerDock, self.historyDock)

    def run_startup_script(self):
        """ Run the startup script that can be used for configuration.
        """
        if not os.path.isfile(self.startup_script):
            content = ('# Startup script for Spyke Viewer\n'
                       '# "viewer" is the main window')
            with open(self.startup_script, 'w') as f:
                f.write(content)

        try:
            with open(self.startup_script, 'r') as f:
                # We turn all encodings to UTF-8, so remove encoding
                # comments manually
                lines = f.readlines()
                if lines:
                    if re.findall('coding[:=]\s*([-\w.]+)', lines[0]):
                        lines.pop(0)
                    elif re.findall('coding[:=]\s*([-\w.]+)', lines[1]):
                        lines.pop(1)
                    source = ''.join(lines).decode('utf-8')
                    code = compile(source, self.startup_script, 'exec')
                    exec(code, {'viewer': self})
        except Exception:
            logger.warning('Error during execution of startup script ' +
                           self.startup_script + ':\n' +
                           traceback.format_exc() + '\n')

    ##### Interactive Python #############################################
    def get_console_objects(self):
        """ Return a dictionary of objects that should be included in the
        console on startup. These objects will also not be displayed in
        variable explorer. Override this function in domain-specific
        subclasses, e.g. for imports.
        """
        import numpy
        import scipy
        import matplotlib.pyplot as plt
        import guiqwt.pyplot as guiplt
        plt.ion()
        guiplt.ion()

        return {'np': numpy, 'sp': scipy, 'plt': plt, 'guiplt': guiplt}

    def init_python(self):
        """ Initialize the Python docks: console, history and variable
        explorer.
        """
        class StreamDuplicator():
            def __init__(self, out_list):
                self.outs = out_list

            def write(self, s):
                for o in self.outs:
                    o.write(s)

            def flush(self):
                for o in self.outs:
                    if hasattr(o, 'flush'):
                        o.flush()

        # Fixing autocompletion bugs in the internal shell
        class FixedInternalShell(InternalShell):
            def __init__(self, *args, **kwargs):
                super(FixedInternalShell, self).__init__(*args, **kwargs)

            def show_completion_list(self, completions, completion_text="",
                                     automatic=True):
                if completions is None:
                    return
                super(FixedInternalShell, self).show_completion_list(
                    completions, completion_text, automatic)

            def get_dir(self, objtxt):
                if not isinstance(objtxt, (str, unicode)):
                    return
                return super(FixedInternalShell, self).get_dir(objtxt)

        # Console
        msg = ('current and selections can be used to access selected data'
               '\n\nModules imported at startup: ')
        ns = self.get_console_objects()
        excludes = ['execfile', 'guiplt', 'help', 'raw_input', 'runfile']
        first_item = True
        for n, o in ns.iteritems():
            if type(o) == type(sys):
                if not first_item:
                    msg += ', '
                first_item = False
                msg += o.__name__
                if n != o.__name__:
                    msg += ' as ' + n

                excludes.append(n)

        ns['current'] = self.provider
        ns['selections'] = self.selections

        font = QFont("Monospace")
        font.setStyleHint(font.TypeWriter, font.PreferDefault)
        if not platform.system() == 'Darwin':
            font.setPointSize(9)
        self.console = FixedInternalShell(
            self.consoleDock, namespace=ns, multithreaded=False,
            message=msg, max_line_count=10000, font=font)
        #self.console.clear_terminal()

        self.console.set_codecompletion_auto(True)
        self.console.set_calltips(True)
        self.console.setup_calltips(size=600, font=font)
        self.console.setup_completion(size=(370, 240), font=font)

        self.consoleDock.setWidget(self.console)

        # Variable browser
        self.browser = NamespaceBrowser(self.variableExplorerDock)
        self.browser.set_shellwidget(self.console)
        self.browser.setup(
            check_all=True, exclude_private=True,
            exclude_uppercase=False, exclude_capitalized=False,
            exclude_unsupported=False, truncate=False, minmax=False,
            collvalue=False, remote_editing=False, inplace=False,
            autorefresh=False,
            excluded_names=excludes)
        self.variableExplorerDock.setWidget(self.browser)

        # History
        self.history = CodeEditor(self.historyDock)
        self.history.setup_editor(linenumbers=False, language='py',
                                  scrollflagarea=False)
        self.history.setReadOnly(True)
        self.history.set_text('\n'.join(self.console.history))
        self.history.set_cursor_position('eof')
        self.historyDock.setWidget(self.history)
        self.console.connect(self.console, SIGNAL("refresh()"),
                             self._append_python_history)

        # Duplicate stdout, stderr and logging for console
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(logging.WARNING)
        logger.addHandler(ch)

        # Not using previous stdout, only stderr. Using StreamDuplicator
        # because spyder stream does not have flush() method...
        sys.stdout = StreamDuplicator([sys.stdout])
        sys.stderr = StreamDuplicator([sys.stderr, sys.__stderr__])

    def _append_python_history(self):
        self.browser.refresh_table()
        self.history.append('\n' + self.console.history[-1])
        self.history.set_cursor_position('eof')

    def create_ipython_kernel(self):
        """ Create a new IPython kernel. Does nothing if a kernel already
        exists.
        """
        if not ipy.ipython_available or self.ipy_kernel:
            return

        stdout = sys.stdout
        stderr = sys.stderr
        dishook = sys.displayhook

        # Don't print message about kernel to console
        sys.stderr = sys.__stderr__

        self.ipy_kernel = ipy.IPythonLocalKernelApp.instance()
        self.ipy_kernel.initialize()

        ns = self.ipy_kernel.get_user_namespace()
        ns['current'] = self.provider
        ns['selections'] = self.selections

        # OMG it's a hack! (to duplicate stdout, stderr)
        ipyout = sys.stdout
        ipyerr = sys.stderr
        ipydishook = sys.displayhook

        def write_stdout(s):
            ipyout._oldwrite(s)
            ipyout.flush()
            stdout.write(s)

        def write_stderr(s):
            ipyerr._oldwrite(s)
            ipyerr.flush()
            stderr.write(s)

        def displayhook(s):
            ipydishook(s)
            dishook(s)

        ch = logging.StreamHandler(ipyerr)
        ch.setLevel(logging.WARNING)
        logger.addHandler(ch)

        sys.stdout._oldwrite = sys.stdout.write
        sys.stdout.write = write_stdout
        sys.stderr._oldwrite = sys.stderr.write
        sys.stderr.write = write_stderr
        sys.displayhook = displayhook

    def on_variableExplorerDock_visibilityChanged(self, visible):
        if visible:
            self.browser.refresh_table()

    def on_historyDock_visibilityChanged(self, visible):
        if visible:
            self.history.set_cursor_position('eof')

    @pyqtSignature("")
    def on_actionIPython_triggered(self):
        if not ipy.ipython_available:
            return
        self.create_ipython_kernel()
        ipy.connect_qtconsole(self.ipy_kernel.connection_file)

    ##### Selections #####################################################
    def on_menuSelections_mousePressed(self, event):
        if event.button() == Qt.LeftButton:
            action = self.menuSelections.actionAt(event.pos())
            if action:
                selection = action.data()
                if selection:
                    self.seldrag_start_pos = event.pos()
                    self.seldrag_selection = selection
        else:
            self.seldrag_start_pos = None
            self.seldrag_selection = None
            self.seldrag_target = None
        QMenu.mousePressEvent(self.menuSelections, event)

    def on_menuSelections_mouseMoved(self, event):
        if event.buttons() & Qt.LeftButton and self.seldrag_start_pos:
            if ((event.pos() - self.seldrag_start_pos).manhattanLength() >=
                    QApplication.startDragDistance()):
                drag = QDrag(self.menuSelections)
                data = QMimeData()
                data.setText(self.seldrag_selection.name)
                drag.setMimeData(data)
                drag.exec_()
                self.seldrag_start_pos = None
                self.seldrag_selection = None
                self.seldrag_target = None
        QMenu.mouseMoveEvent(self.menuSelections, event)

    def on_menuSelections_paint(self, event):
        QMenu.paintEvent(self.menuSelections, event)
        if self.seldrag_target:
            # Paint line where selection will be dropped
            p = QPainter()
            color = QPalette().color(self.menuSelections.foregroundRole())
            pen = QPen(color, 2, Qt.SolidLine)
            p.begin(self.menuSelections)
            p.setPen(pen)
            rect = self.menuSelections.actionGeometry(self.seldrag_target)
            if self.seldrag_target_upper:
                p.drawLine(rect.topLeft(), rect.topRight())
            else:
                p.drawLine(rect.bottomLeft(), rect.bottomRight())
            p.end()

    def _menuSelections_pos_is_drop_target(self, pos):
        """ Return if selection can be dropped at this position and
            prepare information needed for drawing and dropping
        """
        action = self.menuSelections.actionAt(pos)
        if not action or not action.data():
            self.seldrag_target = None
            return False

        self.seldrag_target = action
        rect = self.menuSelections.actionGeometry(action)
        if pos.y() < rect.top() + rect.height() / 2:
            self.seldrag_target_upper = True
        else:
            self.seldrag_target_upper = False
        return True

    def on_menuSelections_dragEnter(self, event):
        event.setDropAction(Qt.MoveAction)
        if self._menuSelections_pos_is_drop_target(event.pos()):
            event.accept()
        else:
            event.ignore()

        QMenu.dragEnterEvent(self.menuSelections, event)

    def on_menuSelections_dragMoved(self, event):
        event.setDropAction(Qt.MoveAction)
        if self._menuSelections_pos_is_drop_target(event.pos()):
            event.accept()
            self.menuSelections.update()
        else:
            event.ignore()

        QMenu.dragMoveEvent(self.menuSelections, event)

    def on_menuSelections_drop(self, event):
        source = self.seldrag_selection
        target = self.seldrag_target.data()
        if source != target:
            self.selections.remove(source)
            target_index = self.selections.index(target)
            if not self.seldrag_target_upper:
                target_index += 1
            self.selections.insert(target_index, source)
            self.populate_selection_menu()

        QMenu.dropEvent(self.menuSelections, event)

    def populate_selection_menu(self):
        self.menuSelections.clear()
        a = self.menuSelections.addAction('New')
        a.triggered.connect(self.on_selection_new)
        a = self.menuSelections.addAction('Clear')
        a.triggered.connect(self.on_selection_clear)
        self.menuSelections.addSeparator()

        for i, s in enumerate(self.selections):
            m = self.menuSelections.addMenu(s.name)
            m.menuAction().setData(s)

            a = m.addAction('Load')
            self.connect(a, SIGNAL('triggered()'),
                         lambda sel=s: self.on_selection_load(sel))

            a = m.addAction('Save')
            self.connect(a, SIGNAL('triggered()'),
                         lambda sel=s: self.on_selection_save(sel))

            a = m.addAction('Rename')
            self.connect(a, SIGNAL('triggered()'),
                         lambda sel=s: self.on_selection_rename(sel))

            a = m.addAction('Remove')
            self.connect(a, SIGNAL('triggered()'),
                         lambda sel=s: self.on_selection_remove(sel))

    def on_selection_load(self, selection):
        self.set_current_selection(selection.data_dict())

    def on_selection_save(self, selection):
        i = self.selections.index(selection)
        self.selections[i] = self.provider_factory(
            self.selections[i].name, self)
        self.populate_selection_menu()

    def on_selection_clear(self):
        if QMessageBox.question(
            self, 'Confirmation',
            'Do you really want to remove all selections?',
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.No:
            return

        del self.selections[:]
        self.populate_selection_menu()

    def on_selection_rename(self, selection):
        (name, ok) = QInputDialog.getText(
            self, 'Edit selection name',
            'New name:', QLineEdit.Normal, selection.name)
        if ok and name:
            selection.name = name
            self.populate_selection_menu()

    def on_selection_remove(self, selection):
        if QMessageBox.question(
                self, 'Confirmation',
                'Do you really want to remove the selection "%s"?' %
                selection.name,
                QMessageBox.Yes | QMessageBox.No) == QMessageBox.No:
            return

        self.selections.remove(selection)
        self.populate_selection_menu()

    def on_selection_new(self):
        self.selections.append(self.provider_factory(
            'Selection %d' % (len(self.selections) + 1), self))
        self.populate_selection_menu()

    def serialize_selections(self):
        sl = list()  # Selection list, current selection as first item
        sl.append(self.provider_factory('__current__', self).data_dict())
        for s in self.selections:
            sl.append(s.data_dict())
        return json.dumps(sl, sort_keys=True, indent=2)

    def save_selections_to_file(self, filename):
        f = open(filename, 'w')
        f.write(self.serialize_selections())
        f.close()

    def load_selections_from_file(self, filename):
        try:
            f = open(filename, 'r')
            p = json.load(f)
            f.close()
            for s in p:
                if not s:
                    continue
                if s['name'] == '__current__':
                    self.set_current_selection(s)
                else:
                    self.add_selection(s)
        except Exception, e:
            self.progress.done()
            QMessageBox.critical(self, 'Error loading selection',
                                 str(type(e).__name__) + ': ' +
                                 str(e).decode('utf8'))
            logger.warning('Error loading selection:\n' +
                           traceback.format_exc() + '\n')
        finally:
            self.populate_selection_menu()

    def load_current_selection(self):
        """ Load the displayed (current) selection from a file.
        """
        current_selection = os.path.join(
            self.selection_path, '.current.sel')
        if os.path.isfile(current_selection):
            self.load_selections_from_file(current_selection)
        else:
            self.populate_selection_menu()

    @pyqtSignature("")
    def on_actionSave_selection_triggered(self):
        d = QFileDialog(self, 'Choose where to save selection',
                        self.selection_path)
        d.setAcceptMode(QFileDialog.AcceptSave)
        d.setNameFilter("Selection files (*.sel)")
        d.setDefaultSuffix('sel')
        if d.exec_():
            filename = str(d.selectedFiles()[0])
        else:
            return

        self.save_selections_to_file(filename)

    @pyqtSignature("")
    def on_actionLoad_selection_triggered(self):
        d = QFileDialog(self, 'Choose selection file',
                        self.selection_path)
        d.setAcceptMode(QFileDialog.AcceptOpen)
        d.setFileMode(QFileDialog.ExistingFile)
        d.setNameFilter("Selection files (*.sel)")
        if d.exec_():
            filename = str(d.selectedFiles()[0])
        else:
            return

        self.load_selections_from_file(filename)

    def set_current_selection(self, data):
        """ Set the current selection based on a dictionary of selection
        data. Override in domain-specific subclasses.
        """
        raise NotImplementedError('No selection model defined!')

    def add_selection(self, data):
        """ Add a selection based on a dictionary of selection data.
        Override in domain-specific subclasses.
        """
        raise NotImplementedError('No selection model defined!')

    ##### Filters ########################################################
    def on_current_filter_changed(self):
        enabled = self.filterDock.current_is_data_item()
        self.actionEditFilter.setEnabled(enabled)
        self.actionDeleteFilter.setEnabled(enabled)
        self.actionCopyFilter.setEnabled(enabled)

    def on_filters_changed(self, filter_type):
        self.filter_populate_function[filter_type]()

    def editFilter(self, copy_item):
        top = self.filterDock.current_filter_type()
        group = self.filterDock.current_filter_group()
        name = self.filterDock.current_name()
        item = self.filterDock.current_item()

        group_filters = None
        if not self.filterDock.is_current_group():
            dialog = FilterDialog(
                self.filterDock.filter_group_dict(), top, group, name,
                item.code, item.combined, item.on_exception, self)
        else:
            group_filters = self.filterDock.group_filters(top, name)
            dialog = FilterGroupDialog(top, name, item.exclusive, self)

        while dialog.exec_():
            if copy_item and name == dialog.name():
                QMessageBox.critical(
                    self, 'Error saving',
                    'Please select a different name for the copied element')
                continue
            try:
                if not copy_item and name != dialog.name():
                    self.filterDock.delete_item(top, name, group)
                if not self.filterDock.is_current_group():
                    self.filterDock.add_filter(
                        dialog.name(), dialog.group(), dialog.type(),
                        dialog.code(), dialog.on_exception(),
                        dialog.combined(), overwrite=True)
                else:
                    self.filterDock.add_filter_group(
                        dialog.name(), dialog.type(), dialog.exclusive(),
                        copy.deepcopy(group_filters), overwrite=True)
                break
            except ValueError as e:
                QMessageBox.critical(self, 'Error saving', str(e))

    def get_active_filters(self, filter_type):
        """ Return a list of active filters for the selected filter type
        """
        return self.filterDock.get_active_filters(filter_type)

    def is_filtered(self, item, filters):
        """ Return if one of the filter functions in the given list
            applies to the given item. Combined filters are ignored.
        """
        for f, n in filters:
            if f.combined:
                continue
            try:
                if not f.function()(item):
                    return True
            except Exception, e:
                if self.show_filter_exceptions:
                    sys.stderr.write(
                        'Exception in filter ' + n + ':\n' + str(e) + '\n')
                if not f.on_exception:
                    return True
        return False

    def filter_list(self, items, filters):
        """ Return a filtered list of the given list with the given filter
            functions. Only combined filters are used.
        """
        if not items:
            return items
        item_type = type(items[0])
        for f, n in filters:
            if not f.combined:
                continue
            try:
                items = [i for i in f.function()(items)
                         if isinstance(i, item_type)]
            except Exception, e:
                if self.show_filter_exceptions:
                    sys.stderr.write(
                        'Exception in filter ' + n + ':\n' + str(e) + '\n')
                if not f.on_exception:
                    return []
        return items

    @pyqtSignature("")
    def on_actionNewFilterGroup_triggered(self):
        top = self.filterDock.current_filter_type()

        dialog = FilterGroupDialog(top, parent=self)
        while dialog.exec_():
            try:
                self.filterDock.add_filter_group(dialog.name(), dialog.type(),
                                                 dialog.exclusive())
                break
            except ValueError as e:
                QMessageBox.critical(self, 'Error creating group', str(e))

    @pyqtSignature("")
    def on_actionNewFilter_triggered(self):
        top = self.filterDock.current_filter_type()
        group = self.filterDock.current_filter_group()

        dialog = FilterDialog(self.filterDock.filter_group_dict(), type=top,
                              group=group, parent=self)
        while dialog.exec_():
            try:
                self.filterDock.add_filter(dialog.name(), dialog.group(),
                                           dialog.type(), dialog.code(),
                                           dialog.on_exception(),
                                           dialog.combined())
                break
            except ValueError as e:
                QMessageBox.critical(self, 'Error creating filter', str(e))

    @pyqtSignature("")
    def on_actionDeleteFilter_triggered(self):
        self.filterDock.delete_current_filter()

    @pyqtSignature("")
    def on_actionEditFilter_triggered(self):
        self.editFilter(False)

    @pyqtSignature("")
    def on_actionCopyFilter_triggered(self):
        self.editFilter(True)

    ##### Plugins ########################################################
    def get_plugin_configs(self):
        """ Return dictionary indexed by (name,path) tuples with configuration
        dictionaries for all plugins.
        """
        indices = self.plugin_model.get_all_indices()
        c = {}

        for idx in indices:
            path = self.plugin_model.data(
                idx, self.plugin_model.FilePathRole)
            plug = self.plugin_model.data(idx, self.plugin_model.DataRole)
            if plug:
                c[(plug.get_name(), path)] = plug.get_parameters()

        return c

    def set_plugin_configs(self, configs):
        """ Takes a dictionary indexed by plugin name with configuration
        dictionaries for plugins and sets configurations of plugins.
        """
        indices = self.plugin_model.get_all_indices()

        d = {}
        for idx in indices:
            path = self.plugin_model.data(
                idx, self.plugin_model.FilePathRole)
            plug = self.plugin_model.data(idx, self.plugin_model.DataRole)
            if plug:
                d[(plug.get_name(), path)] = plug

        for n, c in configs.iteritems():
            if n in d:
                d[n].set_parameters(c)

    def reload_plugins(self, keep_configs=True):
        """ Reloads all plugins.

        :param bool keep_configs: If ``True``, try to restore all plugin
            configuration parameters after reloading.
            Default: ``True``
        """
        old_path = None
        old_configs = {}
        if hasattr(self, 'plugin_model'):
            if keep_configs:
                old_configs = self.get_plugin_configs()
            item = self.pluginsTreeView.currentIndex()
            if item:
                old_path = self.plugin_model.data(
                    item, self.plugin_model.FilePathRole)

        try:
            self.plugin_model = PluginModel()
            for p in self.plugin_paths:
                self.plugin_model.add_path(p)
        except Exception, e:
            QMessageBox.critical(self, 'Error loading plugins', str(e))
            return

        self.pluginsTreeView.setModel(self.plugin_model)

        selected_index = None
        if old_path:
            indices = self.plugin_model.get_indices_for_path(old_path)
            if indices:
                selected_index = indices[0]
                self.pluginsTreeView.setCurrentIndex(selected_index)
        self.pluginsTreeView.expandAll()
        self.pluginsTreeView.selectionModel().currentChanged.connect(
            self.selected_plugin_changed)
        self.selected_plugin_changed(selected_index)
        self.set_plugin_configs(old_configs)

    def _equal_path(self, index, path):
        path_list = list(reversed(path.split('/')))

        while index.row() >= 0:
            if not path_list or index.data() != path_list.pop(0):
                return False
            index = index.parent()
        return True

    def load_plugin_configs(self):
        # Restore closed plugin folders
        settings = QSettings()
        if settings.contains('closedPluginFolders'):
            paths = settings.value('closedPluginFolders')
            if paths is not None:
                folders = self.plugin_model.get_all_folders()
                for p in paths:
                    for f in folders:
                        if self._equal_path(f, p):
                            self.pluginsTreeView.setExpanded(f, False)
                            break

        # Restore plugin configurations
        configs_path = os.path.join(self.data_path, 'plugin_configs.p')
        if os.path.isfile(configs_path):
            with open(configs_path, 'r') as f:
                try:
                    configs = pickle.load(f)
                    self.set_plugin_configs(configs)
                except:
                    pass  # It does not matter if we can't load plugin configs

    def selected_plugin_changed(self, current):
        enabled = True
        if not current:
            enabled = False
        elif not self.plugin_model.data(current, Qt.UserRole):
            enabled = False

        self.actionRunPlugin.setEnabled(enabled)
        self.actionEditPlugin.setEnabled(enabled)
        self.actionConfigurePlugin.setEnabled(enabled)
        self.actionRemotePlugin.setEnabled(enabled)
        self.actionShowPluginFolder.setEnabled(enabled)

    @pyqtSignature("")
    def on_actionRunPlugin_triggered(self):
        ana = self.current_plugin()
        if not ana:
            return

        self._run_plugin(ana)

    def _run_plugin(self, plugin):
        try:
            return plugin.start(self.provider, self.selections)
        except SpykeException, err:
            self.progress.done()
            QMessageBox.critical(self, 'Error executing plugin', str(err))
        except CancelException:
            return None
        except Exception, e:
            self.progress.done()
            # Only print stack trace from plugin on
            tb = sys.exc_info()[2]
            while not ('self' in tb.tb_frame.f_locals and
                       tb.tb_frame.f_locals['self'] == plugin):
                if tb.tb_next is not None:
                    tb = tb.tb_next
                else:
                    break
            traceback.print_exception(type(e), e, tb)
            return None

    @pyqtSignature("")
    def on_actionEditPlugin_triggered(self):
        item = self.pluginsTreeView.currentIndex()
        path = ''
        if item:
            path = self.plugin_model.data(
                item, self.plugin_model.FilePathRole)
        if not path and self.plugin_paths:
            path = self.plugin_paths[0]
        self.pluginEditorDock.add_file(path)

    @pyqtSignature("")
    def on_actionConfigurePlugin_triggered(self):
        ana = self.current_plugin()
        if not ana:
            return

        ana.configure()

    @pyqtSignature("")
    def on_actionRefreshPlugins_triggered(self):
        self.reload_plugins()

    @pyqtSignature("")
    def on_actionNewPlugin_triggered(self):
        self.pluginEditorDock.new_file()

    @pyqtSignature("")
    def on_actionSavePlugin_triggered(self):
        self.pluginEditorDock.save_current()

    @pyqtSignature("")
    def on_actionSavePluginAs_triggered(self):
        self.pluginEditorDock.save_current(True)

    @pyqtSignature("")
    def on_actionShowPluginFolder_triggered(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(
            os.path.dirname(self.current_plugin_path())))

    @pyqtSignature("")
    def on_actionRemotePlugin_triggered(self):
        import subprocess
        import pickle

        selections = self.serialize_selections()
        config = pickle.dumps(self.current_plugin().get_parameters())
        f = open(self.remote_script, 'r')
        code = f.read()
        subprocess.Popen(['python', '-c', '%s' % code,
                          type(self.current_plugin()).__name__,
                          self.current_plugin_path(),
                          selections, '-cf', '-c', config,
                          '-dd', AnalysisPlugin.data_dir])

    @pyqtSignature("")
    def on_actionEdit_Startup_Script_triggered(self):
        self.pluginEditorDock.add_file(self.startup_script)

    @pyqtSignature("")
    def on_actionRestorePluginConfigurations_triggered(self):
        self.reload_plugins(False)

    def on_pluginsTreeView_doubleClicked(self, index):
        self.on_actionRunPlugin_triggered()

    def on_pluginsTreeView_customContextMenuRequested(self, pos):
        self.menuPlugins.popup(self.pluginsTreeView.mapToGlobal(pos))

    def plugin_saved(self, path):
        if path == self.startup_script:
            return

        plugin_path = os.path.normpath(os.path.realpath(path))
        in_dirs = False
        for p in self.plugin_paths:
            directory = os.path.normpath(os.path.realpath(p))
            if os.path.commonprefix([plugin_path, directory]) == directory:
                in_dirs = True
                break

        if in_dirs:
            self.reload_plugins()
        elif self.config['ask_plugin_path']:
            if QMessageBox.question(self, 'Warning',
                                    'The file "%s"' % plugin_path +
                                    ' is not in the currently valid plugin '
                                    'directories. Do you want to open the '
                                    'directory'
                                    'settings now?',
                                    QMessageBox.Yes | QMessageBox.No) == \
                    QMessageBox.No:
                return
            self.on_actionSettings_triggered()

    def current_plugin(self):
        """ Return the currently selected plugin object
        """
        item = self.pluginsTreeView.currentIndex()
        if not item:
            return None

        return self.plugin_model.data(item, self.plugin_model.DataRole)

    def current_plugin_path(self):
        """ Return the path of the file from which the currently selected
        plugin has been loaded.
        """
        item = self.pluginsTreeView.currentIndex()
        if not item:
            return None

        return self.plugin_model.data(item, self.plugin_model.FilePathRole)

    def get_plugin(self, name):
        """ Get plugin with the given name. Raises a SpykeException if
        multiple plugins with this name exist. Returns None if no such
        plugin exists.
        """
        plugins = self.plugin_model.get_plugins_for_name(name)
        if not plugins:
            return None
        if len(plugins) > 1:
            raise SpykeException('Multiple plugins named "%s" exist!' % name)

        return plugins[0]

    def start_plugin(self, name):
        """ Start first plugin with given name and return result of start()
        method. Raises a SpykeException if not exactly one plugins with
        this name exist.
        """
        plugins = self.plugin_model.get_plugins_for_name(name)
        if not plugins:
            return None
        if len(plugins) > 1:
            raise SpykeException('Multiple plugins named "%s" exist!' % name)

        return self._run_plugin(plugins[0])

    def on_file_available(self, available):
        """ Callback when availability of a file for a plugin changes.
        """
        self.actionSavePlugin.setEnabled(available)
        self.actionSavePluginAs.setEnabled(available)

    ##### General housekeeping ###########################################
    @pyqtSignature("")
    def on_actionSettings_triggered(self):
        settings = SettingsWindow(self.selection_path, self.filter_path,
                                  AnalysisPlugin.data_dir, self.remote_script, self.plugin_paths,
                                  self)

        if settings.exec_() == settings.Accepted:
            self.selection_path = settings.selection_path()
            self.filter_path = settings.filter_path()
            self.remote_script = settings.remote_script()
            self.plugin_paths = settings.plugin_paths()
            if self.plugin_paths:
                self.pluginEditorDock.set_default_path(self.plugin_paths[-1])
            self.reload_plugins()

    @pyqtSignature("")
    def on_actionExit_triggered(self):
        self.close()

    @pyqtSignature("")
    def on_actionAbout_triggered(self):
        from .. import __version__

        about = QMessageBox(self)
        about.setWindowTitle(u'About Spyke Viewer ' + __version__)
        about.setTextFormat(Qt.RichText)
        about.setIconPixmap(QPixmap(':/Application/Main'))
        about.setText(
            u'Spyke Viewer is an application for navigating, '
            u'analyzing and visualizing electrophysiological datasets.<br>'
            u'<br><a href=http://www.ni.tu-berlin.de/software/spykeviewer>'
            u'www.ni.tu-berlin.de/software/spykeviewer</a>'
            u'<br><br>Copyright 2012 \xa9 Robert Pr\xf6pper<br>'
            u'Neural Information Processing Group<br>'
            u'TU Berlin, Germany<br><br>'
            u'Licensed under the terms of the BSD license.<br>'
            u'Icons from the Crystal Project '
            u'(\xa9 2006-2007 Everaldo Coelho)')
        about.show()

    @pyqtSignature("")
    def on_actionDocumentation_triggered(self):
        webbrowser.open('http://spyke-viewer.readthedocs.org')

    def closeEvent(self, event):
        """ Saves filters, plugin configs and GUI state.
        """
        if not self.pluginEditorDock.close_all():
            event.ignore()
            return

        # Ensure that selection folder exists
        if not os.path.exists(self.selection_path):
            try:
                os.makedirs(self.selection_path)
            except OSError:
                QMessageBox.critical(
                    self, 'Error', 'Could not create selection directory!')

        self.save_selections_to_file(
            os.path.join(self.selection_path, '.current.sel'))

        # Ensure that filters folder exists
        if not os.path.exists(self.filter_path):
            try:
                os.makedirs(self.filter_path)
            except OSError:
                QMessageBox.critical(self, 'Error',
                                     'Could not create filter directory!')
        self.filterDock.save()

        # Save GUI configuration (docks and toolbars)
        settings = QSettings()
        settings.setValue('windowGeometry', self.saveGeometry())
        settings.setValue('windowState', self.saveState())

        # Save further configurations
        settings.setValue('pluginPaths', self.plugin_paths)
        settings.setValue('selectionPath', self.selection_path)
        settings.setValue('filterPath', self.filter_path)
        settings.setValue('remoteScript', self.remote_script)

        # Save plugin configurations
        configs = self.get_plugin_configs()
        configs_path = os.path.join(self.data_path, 'plugin_configs.p')
        with open(configs_path, 'w') as f:
            pickle.dump(configs, f)

        # Save closed plugin folders
        folders = self.plugin_model.get_all_folders()
        paths = []
        for f in folders:
            if self.pluginsTreeView.isExpanded(f):
                continue
            path = [f.data()]
            p = f.parent()
            while p.row() >= 0:
                path.append(p.data())
                p = p.parent()

            paths.append('/'.join(reversed(path)))
        print >> sys.stderr, paths
        settings.setValue('closedPluginFolders', paths)

        super(MainWindow, self).closeEvent(event)

        # Prevent lingering threads
        self.fileTreeView.setModel(None)
        del self.file_system_model
        self.pluginsTreeView.setModel(None)
        del self.plugin_model