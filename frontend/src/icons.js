// 字符串名 → 图标组件的查找表。
// EmptyState / StatCard 等公共组件接受 icon="Name" 字符串 prop，
// 内部通过此表解析为组件，替代已移除的全局图标注册（main.js）。
// 只包含实际被字符串引用的图标；其余图标由各视图/组件直接 import。
import {
  Box,
  ChatDotRound,
  ChatLineRound,
  Collection,
  Connection,
  Cpu,
  DataLine,
  Document,
  DocumentChecked,
  Files,
  Grid,
  Key,
  List,
  Lock,
  MagicStick,
  RefreshLeft,
  Search,
  Setting,
  Timer,
  User,
} from '@element-plus/icons-vue'

export const iconMap = {
  Box,
  ChatDotRound,
  ChatLineRound,
  Collection,
  Connection,
  Cpu,
  DataLine,
  Document,
  DocumentChecked,
  Files,
  Grid,
  Key,
  List,
  Lock,
  MagicStick,
  RefreshLeft,
  Search,
  Setting,
  Timer,
  User,
}
