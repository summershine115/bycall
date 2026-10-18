import { reactive } from 'vue'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { useSectionDraft } from '../src/composables/useSectionDraft.js'

test('能力草稿与接口响应独立，取消修改可恢复数组且再次编辑仍会变脏', () => {
  const saved = { allowed_tools: ['read'], skills: null }
  const draft = useSectionDraft(saved)
  draft.form.allowed_tools.push('write')
  assert.deepEqual(saved.allowed_tools, ['read'])
  assert.equal(draft.dirty.value, true)
  draft.reset()
  assert.deepEqual(draft.payload(), saved)
  assert.equal(draft.dirty.value, false)
  draft.form.allowed_tools.push('other')
  draft.reset()
  assert.deepEqual(draft.form.allowed_tools, ['read'])
})

test('保存一个分区不清除另一分区草稿，payload 保留真实字段与清空语义', () => {
  const persona = useSectionDraft({ system_prompt_version: 1, model_id: 2 })
  const capability = useSectionDraft({ skills: ['a'], allowed_tools: ['read'] })
  persona.form.model_id = null
  capability.form.skills.push('b')
  persona.accept(persona.payload())
  assert.deepEqual(persona.payload(), { system_prompt_version: 1, model_id: null })
  assert.equal(persona.dirty.value, false)
  assert.equal(capability.dirty.value, true)
  capability.form.allowed_tools = null
  assert.equal(capability.payload().allowed_tools, null)
  capability.reset()
  assert.deepEqual(capability.form.skills, ['a'])
})

// 真实接口响应先放入 ref 后，内部数组也会成为 Proxy。
test('响应对象包含嵌套响应式数组时仍可建立独立基线', () => {
  const response = reactive({ skills: ['search'], nested: { tags: ['public'] } })
  const draft = useSectionDraft({ skills: null, nested: null })
  draft.accept({ skills: response.skills, nested: response.nested })
  assert.equal(draft.dirty.value, false)
  draft.form.skills.push('extra')
  draft.form.nested.tags.push('private')
  assert.deepEqual(response.skills, ['search'])
  draft.reset()
  assert.deepEqual(draft.payload(), { skills: ['search'], nested: { tags: ['public'] } })
})
