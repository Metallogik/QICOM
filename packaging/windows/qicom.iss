; QICOM's Windows installer (Inno Setup 6).
;
; Built by packaging/build_windows_installer.py, which supplies every #define
; below from the staged bundle and qicom/__version__.py; compiling this file by
; hand without them stops at the #error lines rather than producing an
; installer with the wrong version or an empty payload.
;
; The staged directory goes in unchanged, so its SHA256SUMS still verifies in
; the installation folder. User data (projects, logs, preferences) lives under
; %LOCALAPPDATA%\qicom, never next to qicom.exe, so installing into Program
; Files works and uninstalling never deletes a project.

#ifndef SourceDir
  #error SourceDir (the staged bundle directory) is not defined
#endif
#ifndef AppVersion
  #error AppVersion is not defined
#endif
#ifndef NumericVersion
  #error NumericVersion (four dot-separated integers) is not defined
#endif
#ifndef OutputDir
  #error OutputDir is not defined
#endif
#ifndef OutputBaseFilename
  #error OutputBaseFilename is not defined
#endif
#ifndef IconFile
  #error IconFile is not defined
#endif

[Setup]
; Fixed for the life of the product: Windows recognises an upgrade, and lists
; one QICOM under Installed apps, by this identifier alone.
AppId={{6B1D2F4E-8C3A-4E5B-9F70-2A1C3D4E5F60}
AppName=QICOM
AppVersion={#AppVersion}
AppVerName=QICOM {#AppVersion}
AppPublisher=Fabio Mattiussi
AppPublisherURL=https://qicom.ch
AppSupportURL=https://qicom.ch
VersionInfoVersion={#NumericVersion}
VersionInfoProductName=QICOM
VersionInfoDescription=QICOM Setup
; The user picks the folder (DisableDirPage=no). Without administrator rights
; {autopf} is the user's own Programs folder; with them, Program Files.
DefaultDirName={autopf}\QICOM
DisableDirPage=no
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809, the oldest version packaging/DISTRIBUTION.md supports.
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile={#IconFile}
UninstallDisplayIcon={app}\qicom.exe
UninstallDisplayName=QICOM
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
; An upgrade closes a running QICOM first instead of failing on locked files.
CloseApplications=yes
RestartApplications=no
ShowLanguageDialog=auto

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "german"; MessagesFile: "compiler:Languages\German.isl"
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"
Name: "portuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; An upgrade replaces the bundle's libraries as a whole: a file the new
; version no longer ships must not linger and be loaded instead.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\QICOM"; Filename: "{app}\qicom.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\QICOM"; Filename: "{app}\qicom.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\qicom.exe"; Description: "{cm:LaunchProgram,QICOM}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
