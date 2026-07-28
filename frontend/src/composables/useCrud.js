// 标准 CRUD 视图的生命周期壳。
// 管理 loading / dialog / saving 状态和表单重置，
// 具体的 onSave / onDelete / 额外数据加载由视图自行实现。
//
// 用法：
//   const { loading, dialogVisible, editing, saving, form, openCreate, openEdit } = useCrud({
//     emptyForm: () => ({ name: '', description: '' }),
//     toForm: (row) => ({ name: row.name, description: row.description }),
//   })
import { ref, reactive } from 'vue'

/**
 * @param {object} opts
 * @param {() => object} opts.emptyForm - 空表单工厂（openCreate 时重置）
 * @param {(row: object) => object} [opts.toForm] - 行数据 → 表单映射（openEdit 时使用）
 */
export function useCrud({ emptyForm, toForm }) {
  const loading = ref(true)
  const dialogVisible = ref(false)
  const editing = ref(false)
  const saving = ref(false)
  const form = reactive(emptyForm())

  function openCreate() {
    editing.value = false
    Object.assign(form, emptyForm())
    dialogVisible.value = true
  }

  function openEdit(row) {
    editing.value = true
    Object.assign(form, toForm ? toForm(row) : row)
    dialogVisible.value = true
  }

  return { loading, dialogVisible, editing, saving, form, openCreate, openEdit }
}
