; Inno Setup script for Swift Virtual Printer. Built by build.bat (or: ISCC /DMyAppVersion=x.y.z installer.iss)

#define MyAppName "Swift Virtual Printer"
#define MyAppExeName "SwiftVirtualPrinter.exe"
#define MyAppPublisher "db IMMIX"
#ifndef MyAppVersion
  #define MyAppVersion "0.1.0"
#endif

[Setup]
AppId={{6F1C2E3A-8B4D-4E7A-9C51-2D7F0B8A6E13}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppCopyright=Software built by Bikitsos for db IMMIX
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} Setup
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=SwiftVirtualPrinter-Setup-{#MyAppVersion}
SetupIconFile=build\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Start automatically with Windows (in the system tray)"; GroupDescription: "Startup:"
Name: "firewall"; Description: "Allow other computers to print to port 9100 (Windows Firewall rule)"; GroupDescription: "Network:"; Flags: unchecked; Check: IsAdminInstallMode

[Dirs]
; Receipts live next to the exe; let normal users write there even under Program Files. Kept on uninstall.
Name: "{app}\receipts"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{app}\receipts\bin"; Flags: uninsneveruninstall
Name: "{app}\receipts\pdf"; Flags: uninsneveruninstall
Name: "{app}\receipts\json"; Flags: uninsneveruninstall

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion isreadme

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "SwiftVirtualPrinter"; ValueData: """{app}\{#MyAppExeName}"" --minimized"; Flags: uninsdeletevalue; Tasks: startup
; Remove the app's saved settings on uninstall (printed PDFs are kept).
Root: HKCU; Subkey: "Software\SwiftVirtualPrinter"; Flags: uninsdeletekey dontcreatekey

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""{#MyAppName}"" dir=in action=allow protocol=TCP localport=9100 program=""{app}\{#MyAppExeName}"""; Flags: runhidden; Tasks: firewall
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; Flags: runhidden; RunOnceId: "KillApp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""{#MyAppName}"""; Flags: runhidden; RunOnceId: "RemoveFirewallRule"; Check: IsAdminInstallMode
