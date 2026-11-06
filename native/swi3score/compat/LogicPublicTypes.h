// Minimal standalone replacement for the analyzer SDK's LogicPublicTypes.h.
//
// The decode classes include <LogicPublicTypes.h> only for the integer typedefs and
// BitState. This shim defines exactly those, so the decode core builds with no SDK
// dependency. It is placed first on the include path; a build against the SDK uses the
// real header.

#ifndef LOGICPUBLICTYPES
#define LOGICPUBLICTYPES

#include <stddef.h>

typedef signed char S8;
typedef short S16;
typedef int S32;
typedef long long int S64;

typedef unsigned char U8;
typedef unsigned short U16;
typedef unsigned int U32;
typedef unsigned long long int U64;

enum BitState { BIT_LOW, BIT_HIGH };

#endif // LOGICPUBLICTYPES
