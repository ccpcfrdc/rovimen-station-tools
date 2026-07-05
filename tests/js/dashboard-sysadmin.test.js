import { describe, it, expect } from 'vitest';

import * as ctx from '../../dashboard/static/dashboard-sysadmin.js';

describe('_parseCronSchedule', () => {
  const f = ctx._parseCronSchedule;

  it('@reboot', () => {
    expect(f('@reboot')).toEqual({ label: 'At reboot', detail: '' });
  });
  it('@hourly', () => {
    expect(f('@hourly')).toEqual({ label: 'Every hour', detail: 'at :00' });
  });
  it('@daily', () => {
    expect(f('@daily')).toEqual({ label: 'Daily', detail: 'at midnight UTC' });
  });
  it('@midnight (alias for daily)', () => {
    expect(f('@midnight')).toEqual({ label: 'Daily', detail: 'at midnight UTC' });
  });
  it('@weekly', () => {
    expect(f('@weekly')).toEqual({ label: 'Weekly', detail: 'Sunday midnight' });
  });
  it('@monthly', () => {
    expect(f('@monthly')).toEqual({ label: 'Monthly', detail: '1st at midnight' });
  });
  it('every 5 minutes: */5 * * * *', () => {
    expect(f('*/5 * * * *')).toEqual({ label: 'Every 5 min', detail: '' });
  });
  it('every 10 minutes during hour range: */10 4-10 * * *', () => {
    const r = f('*/10 4-10 * * *');
    expect(r.label).toBe('Every 10 min');
    expect(r.detail).toBe('04:00–10:59 UTC');
  });
  it('every N minutes restricted to specific hour: */15 8 * * *', () => {
    const r = f('*/15 8 * * *');
    expect(r.label).toBe('Every 15 min');
    expect(r.detail).toBe('hour 8 UTC');
  });
  it('stepped minute range: 5-55/10 * * * *', () => {
    expect(f('5-55/10 * * * *')).toEqual({ label: 'Every 10 min', detail: '' });
  });
  it('every 2 hours at :30: 30 */2 * * *', () => {
    expect(f('30 */2 * * *')).toEqual({ label: 'Every 2h', detail: 'at :30' });
  });
  it('every minute: * * * * *', () => {
    expect(f('* * * * *')).toEqual({ label: 'Every minute', detail: '' });
  });
  it('hourly at :15: 15 * * * *', () => {
    expect(f('15 * * * *')).toEqual({ label: 'Hourly', detail: 'at :15' });
  });
  it('hourly at :05 (single digit): 5 * * * *', () => {
    expect(f('5 * * * *')).toEqual({ label: 'Hourly', detail: 'at :05' });
  });
  it('weekly on Wednesday: 0 3 * * 3', () => {
    expect(f('0 3 * * 3')).toEqual({ label: 'Weekly (Wed)', detail: 'at 03:00 UTC' });
  });
  it('weekly on Sunday: 30 2 * * 0', () => {
    expect(f('30 2 * * 0')).toEqual({ label: 'Weekly (Sun)', detail: 'at 02:30 UTC' });
  });
  it('daily at 14:30: 30 14 * * *', () => {
    expect(f('30 14 * * *')).toEqual({ label: 'Daily', detail: 'at 14:30 UTC' });
  });
  it('daily at 03:00: 0 3 * * *', () => {
    expect(f('0 3 * * *')).toEqual({ label: 'Daily', detail: 'at 03:00 UTC' });
  });
  it('fallback for complex schedule: 0 3 1,15 * *', () => {
    const r = f('0 3 1,15 * *');
    expect(r.label).toBe('0 3 1,15 * *');
    expect(r.detail).toBe('');
  });
  it('handles extra whitespace', () => {
    expect(f('  @reboot  ')).toEqual({ label: 'At reboot', detail: '' });
  });
});

describe('_logLineLevel', () => {
  const f = ctx._logLineLevel;

  it('classifies ERROR lines', () => {
    expect(f('2026-06-02 ERROR something broke')).toBe('error');
  });
  it('classifies CRITICAL as error', () => {
    expect(f('CRITICAL: disk full')).toBe('error');
  });
  it('classifies WARNING lines', () => {
    expect(f('2026-06-02 WARNING disk 90%')).toBe('warning');
  });
  it('classifies WARN lines', () => {
    expect(f('[WARN] low memory')).toBe('warning');
  });
  it('classifies DEBUG lines', () => {
    expect(f('DEBUG: entering function')).toBe('debug');
  });
  it('defaults to info for normal lines', () => {
    expect(f('Starting server on port 17777')).toBe('info');
  });
  it('case insensitive', () => {
    expect(f('error: something')).toBe('error');
    expect(f('warning: something')).toBe('warning');
    expect(f('debug: something')).toBe('debug');
  });
  it('empty string is info', () => {
    expect(f('')).toBe('info');
  });
});
