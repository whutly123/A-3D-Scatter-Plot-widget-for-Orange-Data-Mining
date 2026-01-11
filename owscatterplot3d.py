import numpy as np
import traceback

# 1. 尝试导入 OpenGL 库
try:
    import pyqtgraph.opengl as gl
    OPENGL_AVAILABLE = True
except ImportError as e:
    OPENGL_AVAILABLE = False
    OPENGL_ERROR = str(e)

from AnyQt.QtWidgets import QSizePolicy, QLabel, QScrollArea, QWidget, QVBoxLayout, QPushButton, QHBoxLayout, QToolTip
from AnyQt.QtCore import Qt, QTimer, QPoint, QEvent
from AnyQt.QtGui import QMatrix4x4, QVector3D, QVector4D, QColor, QMouseEvent

from Orange.data import Table, ContinuousVariable
from Orange.widgets import gui, widget
from Orange.widgets.settings import Setting, ContextSetting, DomainContextHandler
from Orange.widgets.utils.itemmodels import DomainModel
from Orange.widgets.widget import Input, Output

# --- 关键修复：QVector3D 增强版 ---
# 直接继承 QVector3D 以通过 Qt 的类型检查 (如 crossProduct)
# 同时添加 x, y, z 属性以兼容 pyqtgraph 的部分代码
class SafeVector3D(QVector3D):
    def __init__(self, x=0.0, y=0.0, z=0.0):
        # 支持从其他向量复制
        if hasattr(x, 'x') and hasattr(x, 'y') and hasattr(x, 'z'):
            val = x
            if callable(val.x):
                super().__init__(float(val.x()), float(val.y()), float(val.z()))
            else:
                super().__init__(float(val.x), float(val.y), float(val.z))
        else:
            super().__init__(float(x), float(y), float(z))

    # 添加属性访问兼容性 (pyqtgraph 可能尝试访问 .x 而不是 .x())
    @property
    def x_prop(self): return self.x()
    @property
    def y_prop(self): return self.y()
    @property
    def z_prop(self): return self.z()
    
    # 某些 pyqtgraph 版本可能直接访问 .x .y .z 属性
    # 在 PyQt 中，QVector3D 的 x() 是方法。我们不能覆盖它成为属性，否则会破坏 Qt 绑定。
    # 但是，通常 TypeError: 'int' is not callable 是因为 pyqtgraph 内部把 center 初始化成了 (0,0,0) 元组或 0
    # 只要我们始终传递真正的 QVector3D，pyqtgraph 应该能正确处理。
    
    # 为了保险，我们保留 __getitem__ 支持
    def __getitem__(self, i):
        return [self.x(), self.y(), self.z()][i]

class OWScatterPlot3D(widget.OWWidget):
    name = "3D Scatter Plot"
    description = "Visualize data in a three-dimensional scatter plot."
    icon = "icons/ScatterPlot3D.svg"
    priority = 200
    keywords = ["scatter", "3d", "plot", "visualization"]

    class Inputs:
        data = Input("Data", Table)

    class Outputs:
        selected_data = Output("Selected Data", Table)

    settingsHandler = DomainContextHandler()

    # Context Settings
    attr_x = ContextSetting(None)
    attr_y = ContextSetting(None)
    attr_z = ContextSetting(None)
    attr_color = ContextSetting(None)
    attr_size = ContextSetting(None)

    # General Settings
    point_size = Setting(15) 
    point_opacity = Setting(100) 
    show_grid = Setting(True)
    show_axes = Setting(True)
    
    # 新增功能设置
    use_compat_mode = Setting(True)
    use_white_bg = Setting(False) # 白色背景
    show_ticks = Setting(False)   # 显示刻度

    # Selection
    selection = Setting(set(), schema_only=True) # 存储选中行的索引

    def __init__(self):
        super().__init__()

        self.data = None
        self.scatterplot_item = None
        self.selection_item = None # 用于显示选中高亮
        self.grid_item = None
        self.axis_item = None
        self.tick_items = [] # 存储刻度标签
        
        # 存储原始数据范围用于显示刻度
        self.data_ranges = {'x': (0, 1), 'y': (0, 1), 'z': (0, 1)}
        # 存储当前点的3D坐标和对应的行索引，用于Tooltip查找和点击选择
        self.current_points_3d = None 
        self.current_indices = None # 映射: visual_index -> data_row_index
        self.current_colors = None # 存储当前颜色用于高亮时恢复原色
        self.current_sizes = None  # 存储当前大小
        
        # --- GUI Layout ---
        self.controlArea.setFixedWidth(250)
        
        # Axes
        box_axes = gui.vBox(self.controlArea, "Axes")
        self.xy_model = DomainModel(DomainModel.MIXED, placeholder="None")
        
        self.cb_attr_x = gui.comboBox(
            box_axes, self, "attr_x", label="Axis X:",
            callback=self.replot, model=self.xy_model
        )
        self.cb_attr_y = gui.comboBox(
            box_axes, self, "attr_y", label="Axis Y:",
            callback=self.replot, model=self.xy_model
        )
        self.cb_attr_z = gui.comboBox(
            box_axes, self, "attr_z", label="Axis Z:",
            callback=self.replot, model=self.xy_model
        )

        # Appearance
        box_appear = gui.vBox(self.controlArea, "Appearance")
        self.c_model = DomainModel(DomainModel.MIXED, placeholder="None")
        self.cb_attr_color = gui.comboBox(
            box_appear, self, "attr_color", label="Color:",
            callback=self.replot, model=self.c_model
        )
        
        self.s_model = DomainModel(DomainModel.MIXED, placeholder="None")
        self.cb_attr_size = gui.comboBox(
            box_appear, self, "attr_size", label="Size:",
            callback=self.replot, model=self.s_model
        )

        gui.hSlider(
            box_appear, self, "point_size", label="Point Size",
            minValue=1, maxValue=100, step=1, callback=self.replot
        )

        gui.hSlider(
            box_appear, self, "point_opacity", label="Opacity (%)",
            minValue=10, maxValue=100, step=10, callback=self.replot
        )

        # Display
        box_display = gui.vBox(self.controlArea, "Display")
        gui.checkBox(box_display, self, "show_grid", "Show Grid", callback=self.update_scene_elements)
        gui.checkBox(box_display, self, "show_axes", "Show Axes", callback=self.update_scene_elements)
        
        gui.checkBox(box_display, self, "show_ticks", "Show Ticks", callback=self.update_ticks_visibility)
        gui.checkBox(box_display, self, "use_white_bg", "White Background", callback=self.update_background)
        
        gui.checkBox(box_display, self, "use_compat_mode", 
                     "High Compatibility Mode", 
                     callback=self.replot,
                     tooltip="Use geometric shapes instead of pixels. Better compatibility.")
        
        # Actions
        box_action = gui.vBox(self.controlArea, "Actions")
        self.btn_reset = QPushButton("Reset Camera View")
        self.btn_reset.clicked.connect(self.reset_camera)
        box_action.layout().addWidget(self.btn_reset)

        # Info Label
        self.lbl_info = QLabel("No Data")
        self.lbl_info.setStyleSheet("color: #aaa; font-weight: bold; margin-top: 10px;")
        self.lbl_info.setWordWrap(True)
        self.controlArea.layout().addWidget(self.lbl_info)
        
        # Main Area
        self.main_container = gui.vBox(self.mainArea)
        
        if not OPENGL_AVAILABLE:
            self.show_error(f"PyQtGraph OpenGL module could not be imported.\nError: {OPENGL_ERROR}")
            return

        try:
            self.view = gl.GLViewWidget()
            self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            self.main_container.layout().addWidget(self.view)
            
            # 开启鼠标追踪
            self.view.setMouseTracking(True)
            self.view.installEventFilter(self) # 监听事件
            
            self.init_scene()
            self.update_background() # 应用默认背景
            self.reset_camera()
            
        except Exception as e:
            error_msg = "".join(traceback.format_exception(None, e, e.__traceback__))
            self.show_error(f"Error initializing 3D View:\n{error_msg}")

    def show_error(self, message):
        lbl = QLabel(message)
        lbl.setStyleSheet("color: red; font-family: monospace;")
        lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidget(lbl)
        scroll.setWidgetResizable(True)
        self.main_container.layout().addWidget(scroll)

    def init_scene(self):
        self.grid_item = gl.GLGridItem()
        self.grid_item.setSize(20, 20, 0)
        self.grid_item.setSpacing(1, 1, 0)
        self.grid_item.translate(0, 0, -10) 
        self.view.addItem(self.grid_item)

        self.axis_item = gl.GLAxisItem()
        self.axis_item.setSize(10, 10, 10)
        self.view.addItem(self.axis_item)
        
        self.scatterplot_item = None
        self.update_scene_elements()

    def reset_camera(self):
        if hasattr(self, 'view'):
            # 使用继承自 QVector3D 的 SafeVector3D
            center = SafeVector3D(0, 0, 0)
            self.view.setCameraPosition(distance=35, elevation=30, azimuth=45)
            self.view.opts['center'] = center
            self.view.update()

    def update_background(self):
        """切换背景颜色（黑/白）"""
        if hasattr(self, 'view'):
            if self.use_white_bg:
                self.view.setBackgroundColor('w')
                # 白色背景下，网格设为深灰色
                self.grid_item.setColor(QColor(50, 50, 50, 100))
                # 提示文字颜色
                self.lbl_info.setStyleSheet("color: #333; font-weight: bold; margin-top: 10px;")
            else:
                self.view.setBackgroundColor('k')
                # 黑色背景下，网格设为浅灰色
                self.grid_item.setColor(QColor(255, 255, 255, 100))
                self.lbl_info.setStyleSheet("color: #aaa; font-weight: bold; margin-top: 10px;")
            
            # 更新刻度颜色
            self.update_ticks()
            self.view.update()

    def update_scene_elements(self):
        if hasattr(self, 'grid_item'): self.grid_item.setVisible(self.show_grid)
        if hasattr(self, 'axis_item'): self.axis_item.setVisible(self.show_axes)
        self.view.update()

    def update_ticks_visibility(self):
        """只切换刻度可见性，不重绘"""
        visible = self.show_ticks
        for item in self.tick_items:
            item.setVisible(visible)
        self.view.update()

    def update_ticks(self):
        """重新生成刻度标签"""
        # 清除旧刻度
        for item in self.tick_items:
            try:
                self.view.removeItem(item)
            except:
                pass
        self.tick_items = []

        if not self.show_ticks:
            return

        # 刻度颜色：根据背景反色
        text_color = QColor('black') if self.use_white_bg else QColor('white')
        
        # 在 -10, 0, 10 三个位置画刻度
        positions = [-10, 0, 10]
        
        def create_label(val_norm, axis_name, pos_3d):
            # 还原真实数值
            r_min, r_max = self.data_ranges[axis_name]
            real_val = (val_norm + 10.0) / 20.0 * (r_max - r_min) + r_min
            
            label_text = f"{real_val:.1f}"
            if abs(real_val) > 1000: label_text = f"{real_val:.1e}"
            
            item = gl.GLTextItem(pos=pos_3d, text=label_text, color=text_color)
            self.view.addItem(item)
            self.tick_items.append(item)

        for p in positions:
            # X轴刻度 (放在 Z=-11, Y=-11 处)
            create_label(p, 'x', [p, -11, -11])
            # Y轴刻度 (放在 Z=-11, X=-11 处)
            create_label(p, 'y', [-11, p, -11])
            # Z轴刻度 (放在 X=-11, Y=-11 处)
            create_label(p, 'z', [-11, -11, p])

    # --- Interaction Logic (Tooltip & Selection) ---
    
    def eventFilter(self, source, event):
        # 鼠标移动 -> Tooltip
        if event.type() == QEvent.MouseMove and self.scatterplot_item is not None: 
            self.handle_tooltip(event.pos())
            
        # 鼠标点击 -> Selection
        if event.type() == QEvent.MouseButtonPress and self.scatterplot_item is not None:
            if event.button() == Qt.LeftButton:
                self.handle_click(event.pos(), event.modifiers())
                
        return super().eventFilter(source, event)

    def find_nearest_point(self, pos, threshold=20.0):
        """
        根据屏幕坐标 pos (QPoint) 查找最近的 3D 点索引
        返回: (visual_index, distance) 或 (None, None)
        """
        if self.current_points_3d is None or len(self.current_points_3d) == 0:
            return None, None
            
        # 数据量过大时跳过计算以防卡顿 (可选)
        if len(self.current_points_3d) > 50000:
            return None, None

        try:
            # 获取矩阵
            view_matrix = self.view.viewMatrix()
            proj_matrix = self.view.projectionMatrix()
            
            # 计算 MVP 矩阵
            mvp = proj_matrix * view_matrix
            w = self.view.width()
            h = self.view.height()
            mx, my = pos.x(), pos.y()
            
            # 构造 (N, 4) 坐标
            points = np.column_stack((self.current_points_3d, np.ones(len(self.current_points_3d))))
            
            # 投影计算
            mvp_np = np.array(mvp.data()).reshape(4, 4)
            clip = points @ mvp_np
            
            # 透视除法
            w_coords = clip[:, 3]
            w_coords[w_coords == 0] = 1.0
            ndc = clip[:, :3] / w_coords[:, np.newaxis]
            
            # 视口变换
            screen_x = (ndc[:, 0] + 1) * w / 2.0
            screen_y = (1 - ndc[:, 1]) * h / 2.0
            
            # 计算距离
            dists = np.sqrt((screen_x - mx)**2 + (screen_y - my)**2)
            
            nearest_idx = np.argmin(dists)
            min_dist = dists[nearest_idx]
            
            if min_dist < threshold:
                return nearest_idx, min_dist
            return None, None
            
        except Exception:
            return None, None

    def handle_tooltip(self, pos):
        idx, _ = self.find_nearest_point(pos)
        if idx is not None:
            real_row_idx = self.current_indices[idx]
            self.show_tooltip_for_row(real_row_idx, pos)
        else:
            QToolTip.hideText()

    def handle_click(self, pos, modifiers):
        idx, _ = self.find_nearest_point(pos)
        
        # 获取对应的数据行索引
        row_idx = self.current_indices[idx] if idx is not None else None
        
        is_ctrl = modifiers & Qt.ControlModifier
        
        if row_idx is not None:
            # 点击了某个点
            if is_ctrl:
                # Ctrl: 切换选中状态
                if row_idx in self.selection:
                    self.selection.remove(row_idx)
                else:
                    self.selection.add(row_idx)
            else:
                # 无Ctrl: 单选
                self.selection = {row_idx}
        else:
            # 点击空白处
            if not is_ctrl:
                self.selection = set()
        
        self.update_selection_visuals()
        self.commit()

    def update_selection_visuals(self):
        """
        更新选中的视觉效果：
        保留原色 + 描边/光晕 (通过在后方绘制大号高亮色点，前方绘制原色点实现)
        """
        # 1. 清理旧的高亮项
        if self.selection_item:
            try:
                self.view.removeItem(self.selection_item)
            except:
                pass
            self.selection_item = None
            
        if not self.selection or self.current_indices is None:
            self.view.update()
            return

        # 2. 找出选中点在当前可视数据中的索引
        # 使用 np.isin 快速过滤
        # mask 是一个布尔数组，长度等于 current_points_3d
        mask = np.isin(self.current_indices, list(self.selection))
        
        if not np.any(mask):
            return # 选中的点可能因为过滤而不显示了

        # 提取选中点的坐标、颜色、大小
        sel_pos = self.current_points_3d[mask]
        sel_orig_colors = self.current_colors[mask]
        sel_orig_sizes = self.current_sizes[mask]
        
        n_sel = len(sel_pos)
        
        # 3. 构建高亮数据
        # 策略：绘制两层点
        # Layer 1 (Halo): 颜色固定(如青色/金色)，尺寸较大，透明度较低
        # Layer 2 (Core): 颜色为原色，尺寸为原尺寸 (为了防止Z-fighting，稍微前移一点点或者依赖绘制顺序)
        
        # 我们可以把两层数据合并到一个 GLScatterPlotItem 中绘制
        
        # 光晕颜色 (Cyan #00FFFF, 稍微透明)
        glow_color = np.array([0.0, 1.0, 1.0, 0.6], dtype=np.float32)
        if self.use_white_bg:
            # 白背景下用深蓝/紫色光晕更明显
             glow_color = np.array([0.5, 0.0, 1.0, 0.6], dtype=np.float32)
             
        glow_colors = np.tile(glow_color, (n_sel, 1))
        
        # 光晕大小 (原大小 * 1.2)
        glow_sizes = sel_orig_sizes * 1.2
        
        # 合并数据: [Halo Points, Core Points]
        # 注意：GLScatterPlotItem 绘制顺序通常是索引顺序，但也受深度测试影响。
        # 为了保证 Halo 在 Core 后面，如果深度相同，先画 Halo。
        
        final_pos = np.vstack((sel_pos, sel_pos))
        final_colors = np.vstack((glow_colors, sel_orig_colors))
        final_sizes = np.hstack((glow_sizes, sel_orig_sizes))
        
        px_mode = not self.use_compat_mode
        
        self.selection_item = gl.GLScatterPlotItem(
            pos=final_pos,
            color=final_colors,
            size=final_sizes,
            pxMode=px_mode
        )
        
        if self.use_compat_mode:
            self.selection_item.setGLOptions('opaque')
        else:
            # translucent 模式通常不仅开启混合，还会禁用深度写入，或者按深度排序
            # 这里设为 translucent 可以让光晕看起来更柔和，也能透过前面的点看到光晕
            self.selection_item.setGLOptions('translucent')
            
        self.view.addItem(self.selection_item)
        self.view.update()

    def commit(self):
        """输出选中数据"""
        if self.data is None:
            self.Outputs.selected_data.send(None)
            return
            
        if not self.selection:
            self.Outputs.selected_data.send(None)
            return
            
        # 将 set 转为 list 并排序（保持顺序通常较好）
        indices = sorted(list(self.selection))
        subset = self.data[indices]
        self.Outputs.selected_data.send(subset)

    def show_tooltip_for_row(self, row_idx, pos):
        if self.data is None: return
        
        inst = self.data[row_idx]
        
        tooltip_text = "<table>"
        attrs = [self.attr_x, self.attr_y, self.attr_z, self.attr_color, self.attr_size]
        attrs = [a for a in attrs if a is not None]
        
        seen = set()
        unique_attrs = []
        for a in attrs:
            if a.name not in seen:
                unique_attrs.append(a)
                seen.add(a.name)
        
        for var in unique_attrs:
            val = str(inst[var])
            tooltip_text += f"<tr><td style='color:gray'>{var.name}:</td><td><b>{val}</b></td></tr>"
        
        tooltip_text += "</table>"
        
        # 如果是选中状态，添加提示
        if row_idx in self.selection:
            tooltip_text += "<br><center><i style='color:cyan'>Selected</i></center>"
            
        global_pos = self.view.mapToGlobal(pos)
        QToolTip.showText(global_pos, tooltip_text, self.view)

    @Inputs.data
    def set_data(self, data):
        self.closeContext()
        self.data = data
        self.selection = set() # 数据改变清空选择
        self.commit()

        if data is None:
            self.xy_model.set_domain(None)
            self.c_model.set_domain(None)
            self.s_model.set_domain(None)
            self.replot()
            return

        self.xy_model.set_domain(data.domain)
        self.c_model.set_domain(data.domain)
        self.s_model.set_domain(data.domain)
        
        try:
            self.openContext(data.domain)
        except Exception:
            self.attr_x = None
            self.attr_y = None
            self.attr_z = None
            self.attr_color = None
            self.attr_size = None
        
        start_idx = 1 if self.xy_model[0] is None else 0
        if not self.attr_x and len(self.xy_model) > start_idx:
            self.attr_x = self.xy_model[start_idx]
        if not self.attr_y and len(self.xy_model) > start_idx + 1:
            self.attr_y = self.xy_model[start_idx + 1]
        if not self.attr_z and len(self.xy_model) > start_idx + 2:
            self.attr_z = self.xy_model[start_idx + 2]
        
        if not self.attr_color and data.domain.class_var:
             if data.domain.class_var in self.c_model:
                 self.attr_color = data.domain.class_var

        self.replot()

    def _get_column_data(self, attr, axis_name='x'):
        if self.data is None:
            return None, None
            
        n_rows = len(self.data)
        if attr is None:
            self.data_ranges[axis_name] = (0, 1) # 默认范围
            return np.zeros(n_rows, dtype=np.float32), np.ones(n_rows, dtype=bool)
        
        try:
            col_data = self.data.get_column_view(attr)[0].astype(np.float32)
        except Exception:
            return None, None
        
        mask = np.isfinite(col_data)
        valid_data = col_data[mask]
        
        if len(valid_data) > 0:
            min_val = np.min(valid_data)
            max_val = np.max(valid_data)
            self.data_ranges[axis_name] = (min_val, max_val)
            
            if max_val != min_val:
                col_data = (col_data - min_val) / (max_val - min_val) * 20.0 - 10.0
            else:
                col_data = col_data - min_val
        else:
            self.data_ranges[axis_name] = (0, 1)
        
        return col_data, mask

    def replot(self):
        # 1. 清理旧数据
        if self.scatterplot_item:
            try: self.view.removeItem(self.scatterplot_item)
            except: pass
            self.scatterplot_item = None
            
        if self.selection_item:
            try: self.view.removeItem(self.selection_item)
            except: pass
            self.selection_item = None

        if self.data is None or not (self.attr_x and self.attr_y):
            self.lbl_info.setText("Status: No Data / Axes Missing")
            return

        # 2. 获取坐标数据
        x, mx = self._get_column_data(self.attr_x, 'x')
        y, my = self._get_column_data(self.attr_y, 'y')
        z, mz = self._get_column_data(self.attr_z, 'z')

        if x is None or y is None or z is None:
            self.lbl_info.setText("Status: Error reading data")
            return

        valid_mask = mx & my & mz
        n_points = np.sum(valid_mask)
        
        if n_points == 0:
            self.lbl_info.setText("Status: 0 valid points")
            return

        try:
            x_v = x[valid_mask]
            y_v = y[valid_mask]
            z_v = z[valid_mask]
            
            # 3. 存储核心数据用于交互
            pos = np.vstack((x_v, y_v, z_v)).transpose()
            pos = np.ascontiguousarray(pos, dtype=np.float32)
            
            self.current_points_3d = pos
            self.current_indices = np.where(valid_mask)[0]

            msg = f"Points: {n_points} | Mode: {'Compat' if self.use_compat_mode else 'Normal'}"
            self.lbl_info.setText(msg)

            # 4. 计算颜色
            alpha = self.point_opacity / 100.0
            colors = np.zeros((n_points, 4), dtype=np.float32)
            colors[:, 0] = 0.0; colors[:, 1] = 1.0; colors[:, 2] = 1.0; colors[:, 3] = alpha # Default Cyan

            if self.attr_color:
                c_data = self.data.get_column_view(self.attr_color)[0]
                c_data = c_data[valid_mask]
                
                if self.attr_color.is_discrete:
                    palette = self.attr_color.colors
                    palette_norm = np.array(palette, dtype=np.float32) / 255.0
                    nan_mask = np.isnan(c_data)
                    indices = c_data.astype(int)
                    # Safe clip
                    indices = np.clip(indices, 0, len(palette)-1)
                    colors[~nan_mask, :3] = palette_norm[indices[~nan_mask]]
                    colors[nan_mask, :3] = 0.5 
                elif self.attr_color.is_continuous:
                    mask = np.isfinite(c_data)
                    if np.any(mask):
                        min_v, max_v = np.min(c_data[mask]), np.max(c_data[mask])
                        if max_v != min_v:
                            norm = (c_data - min_v) / (max_v - min_v)
                        else:
                            norm = np.zeros_like(c_data)
                        # Gradient Blue to Yellow logic or similar
                        colors[mask, 0] = norm[mask]
                        colors[mask, 1] = 0.0
                        colors[mask, 2] = 1.0 - norm[mask]
            
            colors[:, 3] = alpha
            colors = np.ascontiguousarray(colors, dtype=np.float32)
            self.current_colors = colors # 保存颜色用于高亮恢复

            # 5. 计算大小
            base_size = self.point_size
            if self.use_compat_mode:
                final_base_size = base_size / 30.0 
            else:
                final_base_size = base_size

            sizes = np.full(n_points, final_base_size, dtype=np.float32)
            
            if self.attr_size:
                s_data = self.data.get_column_view(self.attr_size)[0]
                s_data = s_data[valid_mask]
                mask = np.isfinite(s_data)
                if np.any(mask):
                    min_v, max_v = np.min(s_data[mask]), np.max(s_data[mask])
                    if max_v != min_v:
                        norm = (s_data - min_v) / (max_v - min_v)
                        sizes[mask] = final_base_size * (0.5 + 1.5 * norm)
            
            sizes = np.ascontiguousarray(sizes, dtype=np.float32)
            self.current_sizes = sizes # 保存大小

            # 6. 创建主散点图项
            px_mode = not self.use_compat_mode
            self.scatterplot_item = gl.GLScatterPlotItem(
                pos=pos, 
                color=colors, 
                size=sizes, 
                pxMode=px_mode
            )
            
            if self.use_compat_mode:
                self.scatterplot_item.setGLOptions('opaque')
            else:
                self.scatterplot_item.setGLOptions('translucent')

            self.view.addItem(self.scatterplot_item)
            
            # 恢复选区视觉
            self.update_selection_visuals()

            center_x = float(np.mean(pos[:, 0]))
            center_y = float(np.mean(pos[:, 1]))
            center_z = float(np.mean(pos[:, 2]))
            
            center = SafeVector3D(center_x, center_y, center_z)
            self.view.opts['center'] = center
            
            self.update_ticks()
            
            QTimer.singleShot(50, self.view.update)

        except Exception as e:
            self.lbl_info.setText(f"Render Error: {str(e)}")
            print(e) 

    update_graph = replot
    update_colors = replot
    update_sizes = replot

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'view') and self.view:
            self.view.update()

if __name__ == "__main__":
    from Orange.widgets.utils.widgetpreview import WidgetPreview
    data = Table("iris")
    WidgetPreview(OWScatterPlot3D).run(data)