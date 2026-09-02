package main

import (
	"context"
	"time"
)

func ctxForTest() context.Context {
	return context.Background()
}

func timeForTest() time.Time {
	return time.Now()
}
