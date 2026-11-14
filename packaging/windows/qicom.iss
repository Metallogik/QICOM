; QICOM's Windows installer (Inno Setup 6): the classic setup wizard.
;
; Welcome (what QICOM is) -> licence -> About QICOM -> destination folder
; (Program Files by default) -> ready -> progress bar with percentage ->
; finish, with "Launch QICOM". English throughout, with the QICOM logo on the
; wizard and the QICOM icon on the setup file itself.
;
; Built by packaging/build_windows_installer.py, which supplies every #define
; below from the staged bundle, qicom/__version__.py and the logo; compiling
; this file by hand without them stops at the #error lines rather than
; producing an installer with the wrong version or an empty payload.
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
#ifndef WizardImages
  #error WizardImages (the welcome page's logo panel, one file per scale) is not defined
#endif
#ifndef WizardSmallImages
  #error WizardSmallImages (the inner pages' logo, one file per scale) is not defined
#endif
#ifndef LicenseFile
  #error LicenseFile is not defined
#endif
#ifndef AboutFile
  #error AboutFile (the About QICOM page) is not defined
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
AppComments=Quality Image Comparator: compare medical images and measure how their quality differs. For research and quality control only.
VersionInfoVersion={#NumericVersion}
VersionInfoProductName=QICOM
VersionInfoDescription=QICOM Setup
; Program Files by default: an installation for every user of the computer,
; which asks for administrator rights. "Install for me only" stays one click
; away in the first dialog for a user without them, and then goes to their
; own Programs folder. The folder itself is always asked (DisableDirPage=no).
DefaultDirName={autopf}\QICOM
DisableDirPage=no
DisableProgramGroupPage=yes
DisableWelcomePage=no
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=commandline dialog
LicenseFile={#LicenseFile}
InfoBeforeFile={#AboutFile}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Windows 10 1809, the oldest version packaging/DISTRIBUTION.md supports.
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile={#IconFile}
WizardStyle=modern
WizardImageFile={#WizardImages}
WizardSmallImageFile={#WizardSmallImages}
UninstallDisplayIcon={app}\qicom.exe
UninstallDisplayName=QICOM - Quality Image Comparator
Compression=lzma2
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMANumBlockThreads=4
; An upgrade closes a running QICOM first instead of failing on locked files.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Welcome to the QICOM Setup Wizard
WelcomeLabel2=This will install [name/ver] on your computer.%n%nQICOM (Quality Image Comparator) compares medical images and measures how their quality differs: objective quality metrics on whole images and regions of interest, heatmaps of the differences, blinded reader evaluations, radiomics and DICOM utility tools.%n%nFor research and quality-control use only - not intended for clinical diagnosis.%n%nIt is recommended that you close all other applications before continuing.
InfoBeforeLabel=Please read the following information about QICOM before continuing.
FinishedLabel=Setup has finished installing QICOM on your computer. You can start it from the Start menu (and from the desktop, if you chose a shortcut).

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; An upgrade replaces the bundle's libraries as a whole: a file the new
; version no longer ships must not linger and be loaded instead.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\QICOM"; Filename: "{app}\qicom.exe"; WorkingDir: "{app}"; Comment: "Quality Image Comparator"
Name: "{autodesktop}\QICOM"; Filename: "{app}\qicom.exe"; WorkingDir: "{app}"; Comment: "Quality Image Comparator"; Tasks: desktopicon

[Run]
Filename: "{app}\qicom.exe"; Description: "{cm:LaunchProgram,QICOM}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[Code]
// The percentage beside the progress bar: Inno Setup draws the bar alone.
var
  ProgressPercent: TNewStaticText;

procedure InitializeWizard;
begin
  ProgressPercent := TNewStaticText.Create(WizardForm);
  ProgressPercent.Parent := WizardForm.InstallingPage;
  ProgressPercent.Left := WizardForm.ProgressGauge.Left;
  ProgressPercent.Top := WizardForm.ProgressGauge.Top + WizardForm.ProgressGauge.Height + ScaleY(8);
  ProgressPercent.Caption := '0%';
end;

procedure CurInstallProgressChanged(CurProgress, MaxProgress: Integer);
var
  Percent: Int64;
begin
  if MaxProgress > 0 then
  begin
    Percent := (Int64(CurProgress) * 100) div MaxProgress;
    ProgressPercent.Caption := IntToStr(Percent) + '%';
  end;
end;
