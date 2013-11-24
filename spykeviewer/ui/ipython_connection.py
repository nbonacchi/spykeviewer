from PyQt4.QtCore import QTimer

ipython_available = False
try:  # Ipython 0.13
    from IPython.zmq.ipkernel import IPKernelApp
    from IPython.frontend.qt.kernelmanager import QtKernelManager
    from IPython.frontend.qt.console.rich_ipython_widget \
        import RichIPythonWidget
    from IPython.config.application import catch_config_error
    from IPython.lib.kernel import connect_qtconsole

    class IPythonLocalKernelApp(IPKernelApp):
        """ A version of the IPython kernel that does not block the Qt event
            loop.
        """
        @catch_config_error
        def initialize(self, argv=None):
            if argv is None:
                argv = []
            super(IPythonLocalKernelApp, self).initialize(argv)
            self.kernel.eventloop = self.loop_qt4_nonblocking
            self.kernel.start()
            self.start()

        def loop_qt4_nonblocking(self, kernel):
            """ Non-blocking version of the ipython qt4 kernel loop """
            kernel.timer = QTimer()
            kernel.timer.timeout.connect(kernel.do_one_iteration)
            kernel.timer.start(1000 * kernel._poll_interval)

        def get_connection_file(self):
            """ Return current kernel connection file. """
            return self.connection_file

        def get_user_namespace(self):
            """ Returns current kernel userspace dict. """
            return self.kernel.shell.user_ns

    ipython_available = True
except ImportError:
    try:  # Ipython >= 1.0
        from IPython.qt.inprocess import QtInProcessKernelManager
        from IPython.qt.console.rich_ipython_widget import RichIPythonWidget

        class IPythonConnection():
            _kernel = None
            _kernel_manager = None
            _kernel_client = None

            def __init__(self):
                self.kernel_manager = QtInProcessKernelManager()
                self.kernel_manager.start_kernel()
                self.kernel = self.kernel_manager.kernel
                self.kernel.gui = 'qt4'

                self.kernel_client = self.kernel_manager.client()
                self.kernel_client.start_channels()

            def get_widget(self, droplist_completion=True):
                completion = 'droplist' if droplist_completion else 'plain'
                widget = RichIPythonWidget(gui_completion=completion)
                widget.kernel_manager = self.kernel_manager
                widget.kernel_client = self.kernel_client
                #widget.setWindowTitle("Spyke Viewer IPython")

                return widget

            def push(self, d):
                self.kernel.shell.push(d)

        ipython_available = True
    except ImportError:
        pass

