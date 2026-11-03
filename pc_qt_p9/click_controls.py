"""Value controls ignore accidental wheel input; parent pages can still scroll."""
from PyQt5.QtWidgets import QComboBox as Combo,QSpinBox as Spin,QDoubleSpinBox as Double


class QComboBox(Combo):
    def wheelEvent(self,event):event.ignore()


class QSpinBox(Spin):
    def wheelEvent(self,event):event.ignore()


class QDoubleSpinBox(Double):
    def wheelEvent(self,event):event.ignore()
