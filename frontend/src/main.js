import { createApp } from 'vue'
// Element Plus 按需引入（unplugin-vue-components + unplugin-auto-import），
// 组件和 API 自动导入；此处只需基础重置样式，组件样式由 resolver 按文件注入。
import 'element-plus/theme-chalk/base.css'
// 按需引入的盲区：ElMessage / ElMessageBox / ElNotification / ElLoading 是 JS 调用（非模板
// 组件），resolver 只对「自动导入」的 API 注入样式；显式 import 时其 CSS 不会被打包，
// 导致弹窗渲染成无样式的黑框。这里统一预加载这几个程序化组件的样式（与 base.css 同路径）。
import 'element-plus/theme-chalk/el-overlay.css'
import 'element-plus/theme-chalk/el-message-box.css'
import 'element-plus/theme-chalk/el-message.css'
import 'element-plus/theme-chalk/el-notification.css'
import 'element-plus/theme-chalk/el-loading.css'
// 设计系统：token → Element 主题覆盖 → 全局基础（顺序不可换）
import './styles/tokens.css'
import './styles/element-overrides.css'
import './styles/base.css'

import App from './App.vue'
import router from './router'

const app = createApp(App)

app.use(router)
// locale 由 App.vue 的 <el-config-provider> 提供（按需引入不走全局插件）

app.mount('#app')
