import comtypes
import comtypes.client as cc

tlb_id = comtypes.GUID("{b361561d-01e4-4251-b6de-0440d12ce6bd}")
cc.GetModule((tlb_id, 1, 0))

import comtypes.gen.ZeissmSEMWrapper as ZeissCOMLib

cp = cc.CreateObject("ZeissmSEMWrapper.Commands", None, None,
                     ZeissCOMLib.ICommands)

ret = cp.Execute("CMD_INITIALIZE")
print("CMD_INITIALIZE returned:", ret)