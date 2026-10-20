import { expect, it } from 'vitest'
import { validAsrCapacity } from './asr-capacity'
import { capacityFixture } from '../test/asrCapacityFixtures'

it('accepts two isolated slots and unknown reservations', () => expect(validAsrCapacity(capacityFixture())).toBe(true))
it.each(['queued', 'groupLimit', 'held', 'remainingCapacity'] as const)('rejects invalid %s', key => {
  expect(validAsrCapacity({ ...capacityFixture(), [key]: -1 })).toBe(false)
})
it('rejects duplicate slots, invalid task ids and contradictory free capacity', () => {
  const value = capacityFixture()
  expect(validAsrCapacity({ ...value, slots: [value.slots[0], value.slots[0]] })).toBe(false)
  expect(validAsrCapacity({ ...value, activeCalls: [{ ...value.activeCalls[0], taskId: 'bad' }] })).toBe(false)
  expect(validAsrCapacity({ ...value, remainingCapacity: 1 })).toBe(false)
})
