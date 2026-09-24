package com.sincetimes.redwar.game.comnunicate
{
   import com.hurlant.crypto.prng.ARC4;
   import com.sincetimes.redwar.game.BASE;
   import com.sincetimes.redwar.game.GLOBAL;
   import com.sincetimes.redwar.game.LocalLogger;
   import com.sincetimes.redwar.game.comnunicate.protocol.RceHeartbeat;
   import com.sincetimes.redwar.game.core.error.ErrorManager;
   import com.sincetimes.redwar.utils.SecNum;
   import com.sincetimes.redwar.utils.debug.LOGGER;
   import flash.events.Event;
   import flash.events.IOErrorEvent;
   import flash.events.ProgressEvent;
   import flash.events.SecurityErrorEvent;
   import flash.events.TimerEvent;
   import flash.external.ExternalInterface;
   import flash.net.Socket;
   import flash.net.URLRequest;
   import flash.net.sendToURL;
   import flash.utils.ByteArray;
   import flash.utils.Endian;
   import flash.utils.IExternalizable;
   import flash.utils.Timer;
   import flash.utils.describeType;
   import flash.utils.getDefinitionByName;
   import flash.utils.getQualifiedClassName;
   
   public class Transport
   {
      
      private static var ip:String;
      
      private static var port:int;
      
      private static var loginCount:int;
      
      public static var isLogin:Boolean;
      
      private static var loginTimer:Timer;
      
      private static var portAry:Array;
      
      public static var isClose:Boolean;
      
      private static var _pendingPkgT:int;
      
      private static var _pendingPkgNum:int;
      
      private static var _pendingTimer:Timer;
      
      private static var _pkgHead:PkgHead;
      
      public static var _socket:Socket;
      
      private static var _onActive:Function;
      
      private static var _decoder:ARC4;
      
      private static var _encoder:ARC4;
      
      private static var _szSid:String;
      
      private static var _loginTime:SecNum;
      
      private static var _loginLevel:int;
      
      private static var _decodeKeyStr:String;
      
      private static var _encodeKeyStr:String;
      
      public static var TRACE_NET_LOG:Boolean = true;
      
      public static var TRACE_NET_LOG_DETAIL:Boolean = false;
      
      public static var dataList:Array = [];
      
      public static var receiveBool:Boolean = true;
      
      private static var _pendingPkg:Array = [];
      
      private static var _pkgMap:PkgMap = new PkgMap();
      
      private static var _waitBytes:uint = 0;
      
      private static var _connectState:int = 0;
      
      private static var _decode:* = false;
      
      private static var _hookReady:Boolean = false;
      
      public function Transport()
      {
         super();
      }
      
      public static function Init(param1:PkgMap = null) : void
      {
         _pkgMap = param1;
         _pkgHead = new PkgHead();
         portAry = [GLOBAL._port,GLOBAL._port1];
         hookBind();
      }
      
      private static function hookBind() : void
      {
         if(_hookReady)
         {
            return;
         }
         try
         {
            if(ExternalInterface.available)
            {
               ExternalInterface.addCallback("__send",hjdzSend);
               ExternalInterface.addCallback("__awake",hjdzAwake);
               ExternalInterface.addCallback("__bldg",hjdzBldg);
               _hookReady = true;
            }
         }
         catch(e:Error)
         {
         }
      }
      
      private static function hookOn() : Boolean
      {
         hookBind();
         if(!ExternalInterface.available)
         {
            return false;
         }
         try
         {
            return ExternalInterface.call("__pktOn") == true;
         }
         catch(e:Error)
         {
         }
         return false;
      }
      
      private static function shortName(param1:*) : String
      {
         var _loc2_:String = getQualifiedClassName(param1);
         var _loc3_:int = _loc2_.lastIndexOf("::");
         return _loc3_ >= 0 ? _loc2_.substr(_loc3_ + 2) : _loc2_;
      }
      
      private static function isMsg(param1:*) : Boolean
      {
         if(param1 == null || param1 is String || param1 is Number || param1 is Boolean || param1 is Array || param1 is ByteArray)
         {
            return false;
         }
         try
         {
            return param1 is IExternalizable;
         }
         catch(e:Error)
         {
         }
         return false;
      }
      
      private static function skipDumpName(param1:String) : Boolean
      {
         if(param1 == null || param1.length == 0)
         {
            return true;
         }
         if(param1 == "prototype" || param1 == "constructor")
         {
            return true;
         }
         if(param1.indexOf("remove") == 0)
         {
            return true;
         }
         if(param1 == "writeToBuffer" || param1 == "readFromBuffer" || param1 == "writeExternal" || param1 == "readExternal" || param1 == "toString")
         {
            return true;
         }
         return false;
      }
      
      private static function dumpVal(param1:*, param2:String, param3:int) : String
      {
         var _loc4_:Array = null;
         var _loc5_:int = 0;
         var _loc6_:int = 0;
         var _loc7_:int = 0;
         var _loc8_:Array = null;
         var _loc9_:* = undefined;
         var _loc10_:String = null;
         if(param1 == null)
         {
            return "";
         }
         if(param1 is Array)
         {
            _loc4_ = param1 as Array;
            if(_loc4_.length == 0)
            {
               return "";
            }
            _loc5_ = param2 == "bagItem" ? 700 : 200;
            _loc6_ = _loc4_.length < _loc5_ ? int(_loc4_.length) : _loc5_;
            _loc8_ = [];
            _loc7_ = 0;
            while(_loc7_ < _loc6_)
            {
               _loc9_ = _loc4_[_loc7_];
               if(_loc9_ == null)
               {
                  _loc8_.push("null");
               }
               else if(isMsg(_loc9_))
               {
                  _loc8_.push(dumpMsg(_loc9_,param3 + 1));
               }
               else
               {
                  _loc8_.push(String(_loc9_));
               }
               _loc7_++;
            }
            _loc10_ = "[" + _loc8_.join(",") + "]";
            if(_loc4_.length > _loc5_)
            {
               _loc10_ = _loc10_.substr(0,_loc10_.length - 1) + ",…共" + _loc4_.length + "项]";
            }
            return _loc10_;
         }
         if(isMsg(param1))
         {
            return dumpMsg(param1,param3 + 1);
         }
         return String(param1);
      }
      
      private static function dumpMsg(param1:*, param2:int) : String
      {
         var _loc3_:XML = null;
         var _loc4_:XML = null;
         var _loc5_:String = null;
         var _loc6_:String = null;
         var _loc7_:* = undefined;
         var _loc8_:String = null;
         var _loc9_:Array = null;
         if(param1 == null)
         {
            return "";
         }
         if(param2 > 6)
         {
            return shortName(param1) + "(…)";
         }
         _loc9_ = [];
         try
         {
            _loc3_ = describeType(param1);
            for each(_loc4_ in _loc3_.accessor)
            {
               _loc5_ = String(_loc4_.@name);
               _loc6_ = String(_loc4_.@access);
               if(!(skipDumpName(_loc5_) || _loc6_ == "writeonly"))
               {
                  try
                  {
                     _loc7_ = param1[_loc5_];
                  }
                  catch(e1:Error)
                  {
                     continue;
                  }
                  _loc8_ = dumpVal(_loc7_,_loc5_,param2);
                  if(!(_loc8_ == null || _loc8_.length == 0))
                  {
                     _loc9_.push(_loc5_ + "=" + _loc8_);
                  }
               }
            }
            for each(_loc4_ in _loc3_.variable)
            {
               _loc5_ = String(_loc4_.@name);
               if(!skipDumpName(_loc5_))
               {
                  try
                  {
                     _loc7_ = param1[_loc5_];
                  }
                  catch(e2:Error)
                  {
                     continue;
                  }
                  _loc8_ = dumpVal(_loc7_,_loc5_,param2);
                  if(!(_loc8_ == null || _loc8_.length == 0))
                  {
                     _loc9_.push(_loc5_ + "=" + _loc8_);
                  }
               }
            }
         }
         catch(e:Error)
         {
         }
         return shortName(param1) + "(" + _loc9_.join(";") + (_loc9_.length > 0 ? ";" : "") + ")";
      }
      
      private static function toHex(param1:ByteArray) : String
      {
         var _loc2_:uint = 0;
         var _loc3_:String = null;
         var _loc4_:String = "";
         var _loc5_:uint = param1.position;
         param1.position = 0;
         while(param1.bytesAvailable)
         {
            _loc2_ = param1.readUnsignedByte();
            _loc3_ = _loc2_.toString(16);
            if(_loc3_.length < 2)
            {
               _loc3_ = "0" + _loc3_;
            }
            _loc4_ += _loc3_;
         }
         param1.position = _loc5_;
         return _loc4_;
      }
      
      private static function hjdzPkt(param1:String, param2:*, param3:ByteArray) : void
      {
         var _loc4_:String = null;
         if(!hookOn())
         {
            return;
         }
         try
         {
            _loc4_ = dumpMsg(param2,0);
            ExternalInterface.call("__pkt",param1,shortName(param2),_loc4_,param3 != null ? toHex(param3) : "");
         }
         catch(e:Error)
         {
         }
      }
      
      private static function hjdzClosed() : void
      {
         if(!hookOn())
         {
            return;
         }
         try
         {
            ExternalInterface.call("__pkt","closed","","","");
         }
         catch(e:Error)
         {
         }
      }
      
      private static function hjdzSend(param1:String, param2:Object) : String
      {
         var _loc3_:Class = null;
         var _loc4_:* = undefined;
         var _loc5_:String = null;
         hookBind();
         if(_connectState != 2 || isClose)
         {
            return "err:notconnected";
         }
         if(!receiveBool)
         {
            return "err:busy";
         }
         try
         {
            _loc3_ = getDefinitionByName("com.sincetimes.redwar.game.comnunicate.protocol::" + param1) as Class;
            _loc4_ = new _loc3_();
            if(param2 != null)
            {
               for(_loc5_ in param2)
               {
                  _loc4_[_loc5_] = param2[_loc5_];
               }
            }
            Send(_loc4_);
            return "ok";
         }
         catch(e:Error)
         {
            return "err:" + e.message;
         }
         return "err:no-callback";
      }
      
      private static function hjdzAwake() : String
      {
         var _loc1_:* = GLOBAL._afktimer;
         GLOBAL._afktimer = GLOBAL.Timestamp();
         return "ok:" + String(_loc1_) + "->" + String(GLOBAL._afktimer);
      }
      
      private static function hjdzBldg() : String
      {
         var _loc1_:* = undefined;
         var _loc2_:* = undefined;
         var _loc3_:* = undefined;
         var _loc4_:int = 0;
         var _loc5_:int = 0;
         var _loc6_:int = 0;
         var _loc7_:int = 0;
         var _loc8_:String = null;
         var _loc9_:Array = null;
         var _loc10_:Object = null;
         var _loc11_:int = 0;
         var _loc12_:int = 0;
         var _loc13_:int = 0;
         var _loc14_:int = 0;
         var _loc15_:int = 0;
         var _loc16_:String = null;
         var _loc17_:* = undefined;
         var _loc18_:Number = NaN;
         var _loc19_:Number = NaN;
         var _loc20_:Number = NaN;
         var _loc21_:Number = NaN;
         var _loc22_:Number = NaN;
         var _loc23_:Number = NaN;
         if(BASE._resources == null)
         {
            return "err:no-res";
         }
         try
         {
            _loc1_ = BASE._resources.r1;
            _loc2_ = BASE._resources.r2;
            _loc4_ = _loc1_ != null && _loc1_.Get != null ? int(_loc1_.Get()) : int(_loc1_);
            _loc5_ = _loc2_ != null && _loc2_.Get != null ? int(_loc2_.Get()) : int(_loc2_);
            _loc6_ = int(BASE._resources.r1max);
            _loc7_ = int(BASE._resources.r2max);
         }
         catch(e0:Error)
         {
            return "err:no-res";
         }
         _loc8_ = "r1=" + _loc4_ + " r1max=" + _loc6_ + " r2=" + _loc5_ + " r2max=" + _loc7_;
         if(BASE._buildingsAll == null)
         {
            return _loc8_;
         }
         _loc9_ = [];
         for each(_loc3_ in BASE._buildingsAll)
         {
            if(_loc3_ != null)
            {
               _loc16_ = "";
               _loc11_ = 0;
               _loc12_ = 0;
               _loc13_ = -1;
               _loc14_ = 1;
               try
               {
                  _loc10_ = BASE.CanUpgrade(_loc3_);
                  if(_loc10_ != null)
                  {
                     _loc11_ = _loc10_.error ? 0 : 1;
                     _loc12_ = _loc10_.needResource ? 1 : 0;
                     if(_loc10_.errorMessage != null)
                     {
                        _loc16_ = String(_loc10_.errorMessage);
                        _loc16_ = _loc16_.split("|").join("/");
                        _loc16_ = _loc16_.split(",").join(" ");
                     }
                  }
               }
               catch(e1:Error)
               {
               }
               try
               {
                  if(_loc3_._hp != null && _loc3_._hpMax != null)
                  {
                     _loc18_ = Number(_loc3_._hp.Get());
                     _loc19_ = Number(_loc3_._hpMax.Get());
                     _loc14_ = _loc19_ > 0 && _loc18_ / _loc19_ >= 0.5 ? 1 : 0;
                  }
               }
               catch(e2:Error)
               {
               }
               try
               {
                  _loc17_ = _loc3_._buildingProps != null ? _loc3_._buildingProps : GLOBAL._buildingProps[_loc3_._type - 1];
                  _loc15_ = int(_loc3_._lvl.Get());
                  if(_loc17_ != null && _loc15_ > 0)
                  {
                     _loc20_ = Number(_loc17_.produce[_loc15_ - 1]);
                     _loc21_ = Number(_loc17_.capacity[_loc15_ - 1]);
                     _loc22_ = Number(_loc17_.cycleTime[_loc15_ - 1]);
                     _loc23_ = _loc3_._stored != null ? Number(_loc3_._stored.Get()) : 0;
                     if(_loc20_ > 0 && _loc22_ > 0)
                     {
                        _loc13_ = int((_loc21_ - _loc23_) * _loc22_ / _loc20_);
                        if(_loc13_ < 0)
                        {
                           _loc13_ = 0;
                        }
                     }
                  }
               }
               catch(e3:Error)
               {
               }
               _loc9_.push("|" + _loc3_._id + "," + _loc3_._type + "," + _loc3_._lvl.Get() + "," + _loc11_ + "," + _loc12_ + "," + _loc13_ + "," + _loc14_ + "," + _loc16_);
            }
         }
         return _loc8_ + _loc9_.join("");
      }
      
      private static function initSocket() : void
      {
         _socket = new Socket();
         _socket.endian = Endian.BIG_ENDIAN;
         _socket.addEventListener(Event.CONNECT,ConnectHandler);
         _socket.addEventListener(Event.CLOSE,CloseHandler);
         _socket.addEventListener(IOErrorEvent.IO_ERROR,IoErrorHandler);
         _socket.addEventListener(SecurityErrorEvent.SECURITY_ERROR,SecurityErrorHandler);
         _socket.addEventListener(ProgressEvent.SOCKET_DATA,SocketDataHandler);
      }
      
      public static function CloseSocket() : void
      {
         isClose = true;
         if(_socket == null)
         {
            return;
         }
         _socket.removeEventListener(Event.CONNECT,ConnectHandler);
         _socket.removeEventListener(Event.CLOSE,CloseHandler);
         _socket.removeEventListener(SecurityErrorEvent.SECURITY_ERROR,SecurityErrorHandler);
         _socket.removeEventListener(ProgressEvent.SOCKET_DATA,SocketDataHandler);
         try
         {
            if(loginTimer != null)
            {
               loginTimer.removeEventListener(TimerEvent.TIMER,onLoginTimer_hd);
               loginTimer.stop();
            }
            _socket.close();
         }
         catch(e:Error)
         {
         }
         loginTimer = null;
         _socket = null;
      }
      
      public static function SetOnActive(param1:Function) : void
      {
         _onActive = param1;
      }
      
      public static function Connect(param1:String, param2:int, param3:Boolean = false) : void
      {
         if(_socket != null)
         {
            Transport.CloseSocket();
         }
         if(_connectState == 0)
         {
            if(portAry.length == 0)
            {
               GLOBAL.ErrorMessage("连接错误",2);
               return;
            }
            isClose = false;
            if(param3)
            {
               LOGGER.log("log","SocketReConnecting!");
            }
            else
            {
               LOGGER.log("log","SocketConnecting!");
            }
            loginTimer = new Timer(10000);
            loginTimer.addEventListener(TimerEvent.TIMER,onLoginTimer_hd);
            initSocket();
            Transport.ip = param1;
            Transport.port = portAry[0];
            _socket.connect(Transport.ip,Transport.port);
            _connectState = 1;
            LOGGER.log("log","connect socket:" + "ip:" + Transport.ip + "port:" + Transport.port);
            LocalLogger.TraceWebLog("connect socket:" + "ip:" + Transport.ip + "port:" + Transport.port);
            portAry.shift();
            if(GLOBAL.checkLoginIdLast(BASE._loginID,"4"))
            {
               sendToURL(new URLRequest(GLOBAL._webPath + "/event.war?" + "log=qq-war-" + BASE._loginID + "-0-" + "connectSocket" + "-" + "start" + "-" + "0" + "-0-0-0"));
            }
         }
      }
      
      public static function GetPkg(param1:Class) : IExternalizable
      {
         return _pkgMap.GetPkg(param1);
      }
      
      public static function ClonePkg(param1:IExternalizable) : void
      {
         _pkgMap.ClonePkg(param1);
      }
      
      public static function Send(param1:IExternalizable) : void
      {
         var _loc2_:String = null;
         _loc2_ = typeof param1;
         var _loc3_:String = getQualifiedClassName(param1);
         _loc2_ = _loc3_.split("::").pop();
         if(_loc2_ != "RceHeartbeat" && _loc2_ != "RceUpdateSave")
         {
            if(TRACE_NET_LOG)
            {
            }
         }
         if(_connectState != 2)
         {
            return;
         }
         if(isClose)
         {
            return;
         }
         var _loc4_:ByteArray = new ByteArray();
         _loc4_.endian = Endian.BIG_ENDIAN;
         param1.writeExternal(_loc4_);
         hjdzPkt("out",param1,_loc4_);
         var _loc5_:int = _pkgMap.GetPkgType(param1);
         if(_loc5_ != 1038 && _loc5_ != 1052 && _loc5_ != 1053 && _loc5_ != 1109)
         {
            _encoder.encrypt(_loc4_);
         }
         var _loc6_:int = int(_loc4_.length);
         var _loc7_:PkgHead = new PkgHead();
         _loc7_.length = _loc6_ + 6;
         _loc7_.type = _loc5_;
         _loc7_.Write(_socket);
         _socket.writeBytes(_loc4_);
         _socket.flush();
         if(!(param1 is RceHeartbeat))
         {
            if(TRACE_NET_LOG)
            {
               LOGGER.log("log","sendlength:" + _loc6_ + "/" + _loc5_);
               LOGGER.log("log","" + param1);
            }
         }
      }
      
      public static function sendWithPending(param1:IExternalizable, param2:uint = 200) : void
      {
         _pendingPkg.push({
            "pkg":param1,
            "t":param2 + _pendingPkgT
         });
         _pendingPkgNum = _pendingPkg.length;
         if(!_pendingTimer)
         {
            _pendingTimer = new Timer(25);
            _pendingTimer.addEventListener(TimerEvent.TIMER,pendingTick);
         }
         _pendingTimer.start();
      }
      
      private static function pendingTick(param1:TimerEvent = null) : void
      {
         _pendingPkgT += 25;
         if(_pendingPkgNum > 0)
         {
            if(_pendingPkg[0].t <= _pendingPkgT)
            {
               Send(_pendingPkg[0].pkg);
               _pendingPkg.shift();
               _pendingPkgNum = _pendingPkg.length;
               if(_pendingPkgNum == 0)
               {
                  _pendingTimer.stop();
               }
            }
         }
      }
      
      public static function OnAuthPass(param1:Boolean) : void
      {
         if(isLogin)
         {
            return;
         }
         loginComplete();
         isLogin = true;
         if(param1)
         {
            _connectState = 2;
            _onActive(BASE._userID);
         }
         else
         {
            _connectState = 0;
            _onActive("-4");
         }
      }
      
      public static function RegisterHandler(param1:Class, param2:Function) : Boolean
      {
         return _pkgMap.SetPkgHandler(param1,param2);
      }
      
      public static function SocketDataHandler(param1:ProgressEvent = null) : void
      {
         var type:uint = 0;
         var len:uint = 0;
         var index:uint = 0;
         var pkg:IExternalizable = null;
         var ba:ByteArray = null;
         var handler:Function = null;
         var tmpbool:Boolean = false;
         var evt:ProgressEvent = param1;
         if(_waitBytes == 0)
         {
            if(_socket.bytesAvailable < 8)
            {
               return;
            }
            _pkgHead.Read(_socket);
            _waitBytes = _pkgHead.length - 6;
         }
         while(_socket.bytesAvailable >= _waitBytes)
         {
            type = _pkgHead.type;
            len = _pkgHead.length;
            index = _pkgHead.index;
            if(TRACE_NET_LOG)
            {
               LOGGER.log("log","Command Type=" + type + "(" + (type - 512) + ")" + ", Length=" + len + ", Index=" + index);
            }
            pkg = _pkgMap.GetPkgByType(type);
            ba = new ByteArray();
            ba.endian = Endian.LITTLE_ENDIAN;
            if(_waitBytes > 0)
            {
               _socket.readBytes(ba,0,_waitBytes);
            }
            _waitBytes = 0;
            ba.position = 0;
            try
            {
               if(type != 533 && type != 552 && type != 553 && type != 560 && type != 643)
               {
                  decode(ba);
                  ba.position = 0;
               }
               pkg.readExternal(ba);
               hjdzPkt("in",pkg,null);
            }
            catch(e:Error)
            {
               ErrorManager.addError("CommandError!!! id=" + type + ":" + e.name + "," + e.message);
            }
            handler = _pkgMap.GetPkgHandler(type);
            if(handler != null)
            {
               tmpbool = true;
               ClonePkg(pkg);
               if(!Transport.receiveBool)
               {
                  tmpbool = false;
               }
               if(tmpbool)
               {
                  if(TRACE_NET_LOG)
                  {
                     if(TRACE_NET_LOG_DETAIL)
                     {
                        LOGGER.log("log","直接执行 : " + pkg);
                     }
                     else
                     {
                        LOGGER.log("log","直接执行 : " + getQualifiedClassName(pkg));
                     }
                  }
                  handler(pkg);
               }
               else
               {
                  dataList.push([handler,pkg,type]);
                  if(TRACE_NET_LOG)
                  {
                     if(TRACE_NET_LOG_DETAIL)
                     {
                        LOGGER.log("log","加入执行队列 : " + pkg);
                     }
                     else
                     {
                        LOGGER.log("log","加入执行队列 : " + getQualifiedClassName(pkg));
                     }
                  }
               }
            }
            if(_socket == null)
            {
               return;
            }
            if(_socket.bytesAvailable < 8)
            {
               _waitBytes = 0;
               return;
            }
            _pkgHead.Read(_socket);
            _waitBytes = _pkgHead.length - 6;
         }
      }
      
      private static function ConnectHandler(param1:Event) : void
      {
         LOGGER.log("log","Connected!!!");
         sendTGWAndA();
         loginTimer.start();
      }
      
      private static function onLoginTimer_hd(param1:Event) : void
      {
         ErrorManager.addError("connect time out！！！！！！！！");
         LocalLogger.TraceWebLog("connect time out！ reconnet");
         _connectState = 0;
         Connect(Transport.ip,Transport.port,true);
         if(_onActive != null)
         {
            _onActive("-3");
         }
      }
      
      public static function loginComplete() : void
      {
         if(loginTimer.running)
         {
            loginTimer.stop();
         }
         loginTimer.removeEventListener(TimerEvent.TIMER,onLoginTimer_hd);
         loginTimer = null;
         LocalLogger.TraceWebLog("loginComplete");
      }
      
      private static function sendTGWAndA() : void
      {
         var _loc2_:String = null;
         ++loginCount;
         LOGGER.log("log","Send LoginCounts：" + loginCount);
         var _loc1_:ByteArray = new ByteArray();
         _loc2_ = "tgw_17_forward\r\nHost: " + GLOBAL._ip + ":" + Transport.port + "\r\n\r\n";
         _loc1_.writeUTFBytes(_loc2_);
         _socket.writeBytes(_loc1_);
         _socket.flush();
         _loc1_.length = 0;
         LOGGER.log("log","Send TGW：" + _loc2_);
         _loc2_ = "a," + BASE._userID + "," + BASE._secret;
         _loc1_.writeShort(_loc2_.length);
         _loc1_.writeUTFBytes(_loc2_);
         _socket.writeBytes(_loc1_);
         _socket.flush();
         LOGGER.log("log","Send logininfo：" + _loc2_);
         LocalLogger.TraceWebLog("Send logininfo：" + "a," + BASE._userID + ",******");
      }
      
      private static function CloseHandler(param1:Event) : void
      {
         _connectState = 0;
         hjdzClosed();
         if(_onActive != null)
         {
            _onActive("-1");
         }
      }
      
      private static function IoErrorHandler(param1:IOErrorEvent) : void
      {
         ErrorManager.addError("IoError : socket断开");
         _connectState = 0;
         hjdzClosed();
         (param1.currentTarget as Socket).removeEventListener(IOErrorEvent.IO_ERROR,IoErrorHandler);
      }
      
      private static function SecurityErrorHandler(param1:SecurityErrorEvent) : void
      {
         ErrorManager.addError("SecurityError : socket断开");
         _connectState = 0;
         Connect(Transport.ip,Transport.port,true);
         if(_onActive != null)
         {
            _onActive("-3");
         }
      }
      
      private static function setArc4(param1:int) : void
      {
         _decode = true;
         if(_szSid == null)
         {
            _decodeKeyStr = BASE._loginID;
            _encodeKeyStr = BASE._loginID;
         }
         else
         {
            _decodeKeyStr = BASE._loginID + param1 + _szSid;
            _encodeKeyStr = _szSid + BASE._loginID + param1;
         }
         var _loc2_:ByteArray = new ByteArray();
         _loc2_.writeUTFBytes(_decodeKeyStr);
         var _loc3_:ByteArray = new ByteArray();
         _loc3_.writeUTFBytes(_encodeKeyStr);
         _decoder = new ARC4(_loc2_,true);
         _encoder = new ARC4(_loc3_,false);
      }
      
      private static function decode(param1:ByteArray) : void
      {
         if(param1.bytesAvailable != 0)
         {
            _decoder.decrypt(param1);
         }
      }
      
      public static function setInitEncryptData(param1:Object) : void
      {
         _loginLevel = param1.level;
         _szSid = param1.sid;
         _loginTime = new SecNum(String(param1.firstLogin) == "true" ? 0 : 1);
         var _loc2_:int = _loginLevel * 100 + _loginTime.Get();
         setArc4(_loc2_);
      }
   }
}
