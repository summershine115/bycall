import { createApp } from 'vue'
// Element Plus 按需引入（unplugin-vue-components + unplugin-auto-import），
// 组件和 API 自动导入；此处只需基础重置样式，组件样式由 resolver 按文件注入。
import 'element-plus/theme-chalk/base.css'
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
