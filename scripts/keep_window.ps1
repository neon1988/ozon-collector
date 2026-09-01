# Фоновый сторож окна коллектора: каждые CHECK_S секунд находит главное окно
# Chrome с ozon-profile и восстанавливает его в центр экрана, показывает и
# поднимает наверх (TOPMOST), не воруя фокус (NOACTIVATE).
# Решает проблему: окно "пропадает", т.к. Chrome/демон прячет его за экран
# (rect -30902,-31924) или создаёт скрытым (SW_HIDE).
param([int]$CheckS = 4)

Add-Type @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public class CapKeeper {
    public delegate bool Ep(IntPtr h, IntPtr l);
    [DllImport("user32.dll")] public static extern bool EnumWindows(Ep cb, IntPtr l);
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr a, int x, int y, int cx, int cy, uint f);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
    [DllImport("user32.dll", SetLastError=true)] public static extern int GetWindowLong(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int n, int v);
    [StructLayout(LayoutKind.Sequential)] public struct R2 { public int L, T, R, B; }
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out R2 r);

    public static IntPtr FindAny(int pid) {
        IntPtr f = IntPtr.Zero;
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            uint p; GetWindowThreadProcessId(h, out p);
            if (p != (uint)pid) return true;
            StringBuilder t = new StringBuilder(256); GetWindowText(h, t, 256);
            if (t.Length > 0) { f = h; return false; }
            return true;
        }, IntPtr.Zero);
        return f;
    }
    public static string Title(IntPtr h) { StringBuilder t = new StringBuilder(256); GetWindowText(h, t, 256); return t.ToString(); }
    public static R2 Rect(IntPtr h) { R2 r; GetWindowRect(h, out r); return r; }
    public static bool Visible(IntPtr h) { return IsWindowVisible(h); }
    public static void Restore(IntPtr h, int x, int y, int w, int hh) {
        int style = GetWindowLong(h, -20);
        SetWindowLong(h, -20, style | 0x08000000); // WS_EX_NOACTIVATE
        ShowWindow(h, 9); // SW_RESTORE
        SetWindowPos((IntPtr)h, (IntPtr)(-1), x, y, w, hh, 0x0002u | 0x0001u | 0x0010u | 0x0040u);
    }
}
'@

while ($true) {
    Start-Sleep -Seconds $CheckS
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*ozon-profile*' -and $_.CommandLine -notlike '*--type=*' })
    foreach ($p in $procs) {
        $h = [CapKeeper]::FindAny($p.ProcessId)
        if ($h -eq [IntPtr]::Zero) { continue }
        $r = [CapKeeper]::Rect($h)
        $w = $r.R - $r.L; $hh = $r.B - $r.T
        $off = ($r.L -lt -1000) -or ($r.T -lt -1000) -or ($r.L -gt 4000) -or ($r.T -gt 2000)
        if ($off) {
            # окно за пределами экрана — вернуть в центр
            $cx = [int](1920/2 - $w/2); $cy = [int](1080/2 - $hh/2)
            [CapKeeper]::Restore($h, $cx, $cy, $w, $hh) | Out-Null
        } elseif (-not [CapKeeper]::Visible($h)) {
            # невидимое — показать
            $cx = [int](1920/2 - $w/2); $cy = [int](1080/2 - $hh/2)
            [CapKeeper]::Restore($h, $cx, $cy, $w, $hh) | Out-Null
        }
    }
}